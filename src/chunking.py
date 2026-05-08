"""
src/chunking.py — Chunk-building logic for the ingestion pipeline.

Responsible for turning the raw outputs of Document Intelligence + the
image-extraction step into a flat list of `ChunkRecord` instances that
can be embedded and indexed.

Three chunk families are produced:

    * text  — sliding-window over per-page text
    * table — one chunk per DI-detected table (kept whole)
    * image — one chunk per extracted image, with a header line that
              carries page + caption so the embedding has context

The module is intentionally free of Azure SDK calls so it can be unit
tested with plain dicts.
"""

from __future__ import annotations

from typing import Any

from src.models import ChunkRecord

# Approx token sizing — we use chars/4 as a cheap proxy.
CHUNK_TOKENS = 500
CHUNK_OVERLAP = 80
MIN_IMAGE_BYTES = 5_000


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------
def chunk_text_pages(
    page_chunks: list[dict],
    doc_id: str,
    *,
    chunk_tokens: int = CHUNK_TOKENS,
    chunk_overlap: int = CHUNK_OVERLAP,
) -> list[ChunkRecord]:
    """Sliding window over per-page text. Each chunk keeps its page number."""
    out: list[ChunkRecord] = []
    max_chars = chunk_tokens * 4
    overlap = chunk_overlap * 4
    for pc in page_chunks:
        text: str = pc["content"]
        page: int = pc["page"]
        if len(text) <= max_chars:
            out.append(ChunkRecord(doc_id=doc_id, page=page, type="text", content=text))
            continue
        start = 0
        while start < len(text):
            end = min(start + max_chars, len(text))
            out.append(
                ChunkRecord(doc_id=doc_id, page=page, type="text", content=text[start:end])
            )
            if end >= len(text):
                break
            start = end - overlap
    return out


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------
def build_table_chunks(tables: list[dict], doc_id: str) -> list[ChunkRecord]:
    """One `ChunkRecord` per DI table (kept whole — no splitting)."""
    return [
        ChunkRecord(doc_id=doc_id, page=t["page"], type="table", content=t["content"])
        for t in tables
    ]


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
def build_image_chunks(images: list[dict[str, Any]], doc_id: str) -> list[ChunkRecord]:
    """Convert the image-extractor output into image `ChunkRecord`s.

    Each image carries a short header (`[Figure on page N — caption]`)
    prepended to the vision-model description so the embedded text has
    enough context to be retrievable on its own.
    """
    chunks: list[ChunkRecord] = []
    for img in images:
        label = "Figure" if img.get("source") == "figure" else "Image"
        caption = img.get("caption") or ""
        header = f"[{label} on page {img['page']}"
        if caption:
            header += f" — {caption}"
        header += "]"
        chunks.append(
            ChunkRecord(
                doc_id=doc_id,
                page=img["page"],
                type="image",
                content=f"{header}: {img['description']}",
                image_url=img["image_url"],
                caption=caption or None,
                source=img.get("source"),
            )
        )
    return chunks


# ---------------------------------------------------------------------------
# Top-level assembly
# ---------------------------------------------------------------------------
def assemble_chunks(
    *,
    doc_id: str,
    text_pages: list[dict],
    tables: list[dict],
    image_chunks: list[ChunkRecord],
    chunk_tokens: int = CHUNK_TOKENS,
    chunk_overlap: int = CHUNK_OVERLAP,
) -> list[ChunkRecord]:
    """Combine text / table / image chunks in a single ordered list.

    Image chunks are passed in already-built (they require I/O — blob
    upload + vision description — that lives in `doc_intelligence.py`).
    """
    text_chunks = chunk_text_pages(
        text_pages,
        doc_id,
        chunk_tokens=chunk_tokens,
        chunk_overlap=chunk_overlap,
    )
    table_chunks = build_table_chunks(tables, doc_id)
    return text_chunks + table_chunks + image_chunks
