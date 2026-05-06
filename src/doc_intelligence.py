"""
src/doc_intelligence.py — Azure Document Intelligence wrapper.

Uses the `prebuilt-layout` model to extract:
* per-page text (paragraphs / lines)
* tables (rendered as markdown)
* image / figure regions (so we know where embedded images live)

Embedded image extraction is handled separately by PyMuPDF in
`src/ingestion.py` because Document Intelligence does not return raw
image bytes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional, TypedDict

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeResult, AnalyzeDocumentRequest
from azure.core.credentials import AzureKeyCredential

import config

if TYPE_CHECKING:  # avoid hard runtime deps for static typing
    from src.blob_client import BlobService
    from src.openai_client import OpenAIService

log = logging.getLogger(__name__)

# Skip thumbnails, bullets, decorative icons.
MIN_IMAGE_BYTES = 5_000


class ExtractedChunk(TypedDict):
    content: str
    page: int
    type: str  # "text" | "table"


class ExtractedImage(TypedDict):
    page: int
    image_url: str
    blob_name: str
    description: str
    ext: str
    size_bytes: int


class DocIntelService:
    """Wrapper around Azure Document Intelligence (prebuilt-layout)."""

    def __init__(
        self,
        endpoint: str = config.DOC_INTEL_ENDPOINT,
        api_key: str | None = config.DOC_INTEL_KEY,
    ) -> None:
        if api_key:
            self._client = DocumentIntelligenceClient(endpoint, AzureKeyCredential(api_key))
        else:
            self._client = DocumentIntelligenceClient(endpoint, config.CREDENTIAL)

    # ------------------------------------------------------------------
    def extract_pdf(self, blob_url: str) -> dict:
        """Run `prebuilt-layout` on a PDF reachable via `blob_url`.

        Returns
        -------
        dict with keys:
            * `pages`: number of pages
            * `text_chunks`: list[ExtractedChunk]   one per page
            * `tables`:      list[ExtractedChunk]   one per detected table
        """
        log.info("Document Intelligence analysing %s", blob_url[:80])
        poller = self._client.begin_analyze_document(
            "prebuilt-layout",
            AnalyzeDocumentRequest(url_source=blob_url),
        )
        result: AnalyzeResult = poller.result()

        text_chunks: list[ExtractedChunk] = []
        for page in result.pages or []:
            lines = [ln.content for ln in (page.lines or []) if ln.content]
            page_text = "\n".join(lines).strip()
            if page_text:
                text_chunks.append(
                    {"content": page_text, "page": page.page_number, "type": "text"}
                )

        tables: list[ExtractedChunk] = []
        for table in result.tables or []:
            md = self._table_to_markdown(table)
            page_no = (
                table.bounding_regions[0].page_number
                if table.bounding_regions
                else 0
            )
            tables.append({"content": md, "page": page_no, "type": "table"})

        return {
            "pages": len(result.pages or []),
            "text_chunks": text_chunks,
            "tables": tables,
        }

    # ------------------------------------------------------------------
    def extract_images(
        self,
        pdf_bytes: bytes,
        doc_id: str,
        blob: "BlobService",
        openai: Optional["OpenAIService"] = None,
        min_image_bytes: int = MIN_IMAGE_BYTES,
    ) -> list[ExtractedImage]:
        """Extract embedded images from a PDF, upload them to Blob, and
        (optionally) verbalize each one via GPT-4o vision.

        Document Intelligence does not return raw image bytes, so we use
        PyMuPDF (`fitz`) to pull them out. Each image is uploaded to the
        provided `blob` container under ``{doc_id}/images/...`` and, if
        an `openai` service is given, described so it can be retrieved
        and shown to users alongside text answers.
        """
        import fitz  # PyMuPDF — local import keeps import cost off cold paths

        out: list[ExtractedImage] = []
        pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            for page_num, page in enumerate(pdf_doc, start=1):
                for idx, img_info in enumerate(page.get_images(full=True)):
                    xref = img_info[0]
                    base = pdf_doc.extract_image(xref)
                    image_bytes: bytes = base["image"]
                    ext: str = base["ext"]
                    if len(image_bytes) < min_image_bytes:
                        continue

                    blob_name = f"{doc_id}/images/page{page_num}_img{idx}.{ext}"
                    blob.upload(blob_name, image_bytes, content_type=f"image/{ext}")
                    image_url = blob.get_url(blob_name)

                    description = "[image]"
                    if openai is not None:
                        try:
                            description = openai.describe_image(image_url)
                        except Exception as e:  # noqa: BLE001
                            log.warning("Vision failed for %s: %s", blob_name, e)

                    out.append(
                        {
                            "page": page_num,
                            "image_url": image_url,
                            "blob_name": blob_name,
                            "description": description,
                            "ext": ext,
                            "size_bytes": len(image_bytes),
                        }
                    )
        finally:
            pdf_doc.close()
        return out

    # ------------------------------------------------------------------
    @staticmethod
    def _table_to_markdown(table) -> str:
        """Render a Document Intelligence table as a markdown table."""
        if not table.cells:
            return ""
        rows = max(c.row_index for c in table.cells) + 1
        cols = max(c.column_index for c in table.cells) + 1
        grid: list[list[str]] = [["" for _ in range(cols)] for _ in range(rows)]
        for c in table.cells:
            grid[c.row_index][c.column_index] = (c.content or "").replace("\n", " ").strip()

        md_lines = [
            "| " + " | ".join(grid[0]) + " |",
            "| " + " | ".join(["---"] * cols) + " |",
        ]
        for r in grid[1:]:
            md_lines.append("| " + " | ".join(r) + " |")
        return "\n".join(md_lines)
