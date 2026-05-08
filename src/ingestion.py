"""
src/ingestion.py — End-to-end PDF ingestion pipeline.

Steps performed by `IngestionPipeline.process_pdf(doc)`:

    1. Download PDF bytes from Blob Storage
    2. Use Document Intelligence (`prebuilt-layout`) to extract per-page
       text + tables
    3. Use PyMuPDF (`fitz`) to extract embedded images, upload each one
       back to Blob, and ask GPT-4o vision to describe it
    4. Smart-chunk the text (sliding window with overlap)
    5. Embed every chunk with text-embedding-ada-002
    6. Index all chunks in Azure AI Search
    7. Update document metadata in Cosmos DB

The pipeline is intentionally synchronous and class-based so it can be
driven from notebooks, the FastAPI app, or the background worker.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.blob_client import BlobService
from src.chunking import (
    CHUNK_OVERLAP,
    CHUNK_TOKENS,
    MIN_IMAGE_BYTES,
    assemble_chunks,
    build_image_chunks,
)
from src.cosmos_client import CosmosService
from src.doc_intelligence import DocIntelService
from src.models import ChunkRecord, DocumentMeta, StageEvent
from src.openai_client import OpenAIService
from src.search_client import SearchService

log = logging.getLogger(__name__)


class IngestionPipeline:
    """Orchestrates Blob → DocIntel → Vision → Search → Cosmos."""

    def __init__(
        self,
        blob: BlobService,
        doc_intel: DocIntelService,
        openai: OpenAIService,
        search: SearchService,
        cosmos: CosmosService,
    ) -> None:
        self.blob = blob
        self.doc_intel = doc_intel
        self.openai = openai
        self.search = search
        self.cosmos = cosmos

    # ------------------------------------------------------------------
    def _stage(
        self,
        doc: DocumentMeta,
        name: str,
        status: str,
        detail: str | None = None,
    ) -> None:
        """Record progress of a pipeline stage and persist on the document."""
        now = datetime.now(timezone.utc).isoformat()
        existing: StageEvent | None = next((s for s in doc.stages if s.name == name), None)
        if existing is None:
            existing = StageEvent(name=name, status=status)
            doc.stages.append(existing)
        existing.status = status  # type: ignore[assignment]
        if status == "running" and not existing.started_at:
            existing.started_at = now
        if status in ("done", "failed"):
            existing.finished_at = now
        if detail is not None:
            existing.detail = detail
        try:
            self.cosmos.save_document(doc)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not persist stage update for %s: %s", doc.id, exc)

    # ------------------------------------------------------------------
    def process_pdf(self, doc: DocumentMeta) -> DocumentMeta:
        """Run the full pipeline on the PDF described by `doc`."""
        log.info("Ingesting doc_id=%s blob=%s", doc.id, doc.blob_name)
        doc.status = "processing"
        doc.error = None
        # initialize pipeline stages (idempotent)
        if not doc.stages:
            for n in ("download", "extract_text", "extract_images", "chunk", "embed", "index", "complete"):
                doc.stages.append(StageEvent(name=n, status="pending"))
        self.cosmos.save_document(doc)

        source_container = doc.container or self.blob.container

        try:
            # 1. Download PDF
            self._stage(doc, "download", "running")
            pdf_bytes = self.blob.download_from(source_container, doc.blob_name)
            self._stage(doc, "download", "done", detail=f"{len(pdf_bytes)} bytes")

            # 2. Document Intelligence — text + tables
            self._stage(doc, "extract_text", "running")
            blob_url = self.blob.url_for(source_container, doc.blob_name)
            extracted = self.doc_intel.extract_pdf(blob_url)
            text_chunks = extracted["text_chunks"]
            tables = extracted["tables"]
            figures = extracted.get("figures", [])
            doc.total_pages = extracted["pages"]
            doc.total_tables = len(tables)
            self._stage(
                doc,
                "extract_text",
                "done",
                detail=f"{doc.total_pages} pages, {doc.total_tables} tables",
            )

            # 3. Hybrid image extraction: DI figures + PyMuPDF rasters
            self._stage(doc, "extract_images", "running")
            image_chunks = self._extract_images(pdf_bytes, doc.id, figures)
            doc.total_images = len(image_chunks)
            self._stage(doc, "extract_images", "done", detail=f"{doc.total_images} images")

            # 4. Smart-chunk text (tables/images stay whole)
            self._stage(doc, "chunk", "running")
            all_chunks: list[ChunkRecord] = assemble_chunks(
                doc_id=doc.id,
                text_pages=text_chunks,
                tables=tables,
                image_chunks=image_chunks,
                chunk_tokens=CHUNK_TOKENS,
                chunk_overlap=CHUNK_OVERLAP,
            )
            self._stage(doc, "chunk", "done", detail=f"{len(all_chunks)} chunks")

            # 5. Embed
            self._stage(doc, "embed", "running")
            if all_chunks:
                texts = [c.content for c in all_chunks]
                # batch embed (ada-002 supports many inputs per call)
                vectors: list[list[float]] = []
                for i in range(0, len(texts), 16):
                    vectors.extend(self.openai.embed(texts[i : i + 16]))
                for c, v in zip(all_chunks, vectors):
                    c.embedding = v
            self._stage(doc, "embed", "done", detail=f"embedded {len(all_chunks)}")

            # 6. Index in AI Search
            self._stage(doc, "index", "running")
            self.search.create_or_update_index()
            self.search.index_chunks(all_chunks)
            self._stage(doc, "index", "done", detail=f"indexed {len(all_chunks)}")

            # 7. Update Cosmos metadata
            doc.total_chunks = len(all_chunks)
            doc.status = "indexed"
            doc.indexed_at = datetime.now(timezone.utc).isoformat()
            self._stage(doc, "complete", "done")
            self.cosmos.save_document(doc)
            log.info("Ingested doc_id=%s chunks=%d", doc.id, doc.total_chunks)
            return doc

        except Exception as e:
            log.exception("Ingestion failed for %s", doc.id)
            doc.status = "failed"
            doc.error = str(e)[:500]
            # mark the running stage as failed
            for s in doc.stages:
                if s.status == "running":
                    s.status = "failed"
                    s.finished_at = datetime.now(timezone.utc).isoformat()
                    s.detail = str(e)[:200]
                    break
            self.cosmos.save_document(doc)
            raise

    # ------------------------------------------------------------------
    def _extract_images(
        self,
        pdf_bytes: bytes,
        doc_id: str,
        figures: list | None = None,
    ) -> list[ChunkRecord]:
        """Hybrid image extraction (DI figures + PyMuPDF rasters), described
        via GPT-4o vision. Captions from DI are used as hints for the vision
        model and prepended to the description when missing.
        """
        images = self.doc_intel.extract_images(
            pdf_bytes,
            doc_id,
            blob=self.blob,
            openai=self.openai,
            figures=figures,
            min_image_bytes=MIN_IMAGE_BYTES,
        )
        return build_image_chunks(images, doc_id)
