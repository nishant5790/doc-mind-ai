"""
src/chunking.py — Section-aware chunk-building for the ingestion pipeline.

Turns the rich Document Intelligence output (paragraphs, sections,
tables, figures) plus the image-extraction step into a flat list of
:class:`ChunkRecord` instances ready to embed and index.

Design goals
------------
* **Section-aware** — chunks never cross section boundaries; every
  chunk carries its `section_path` and `section_id`.
* **Layout-aware** — every chunk records the source `bbox` (PDF
  points, on `page`) so the UI can highlight the region.
* **Parent-child links** — table / image chunks merge in their nearby
  paragraphs *and* point back at the synthetic anchor chunk for their
  section via ``parent_id``.
* **Reading-order preserved** — every chunk has a global
  ``reading_order`` derived from DI's paragraph index.
* **Multi-document safe** — every chunk carries `doc_id`,
  `doc_filename`, and `doc_hash` (sha256 of source bytes) so
  retrieval / deletion / dedup work cleanly across many PDFs.

Practical sizing rules
----------------------
* Text:   400–800 tokens, 10–15% overlap (defaults: 600 / 80)
* Tables: kept whole, prepended with caption + 1–2 nearby paragraphs
* Images: vision description + caption + 1–2 surrounding paragraphs
* Sections: hierarchy preserved via `section_path` + `parent_id`

The module is free of Azure SDK calls so it can be unit tested with
plain dicts.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

import config
from src.models import ChunkRecord

# Approx token sizing — chars/4 as cheap proxy.
CHUNK_TOKENS = config.CHUNK_TOKENS
CHUNK_OVERLAP = config.CHUNK_OVERLAP
MIN_IMAGE_BYTES = config.DOC_INTEL_MIN_IMAGE_BYTES

# A single paragraph longer than this many chars is hard-split into
# overlapping windows so it doesn't blow past the chunk budget.
HARD_SPLIT_HEADROOM = config.CHUNK_HARD_SPLIT_HEADROOM


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _union_bbox(bboxes: Iterable[Optional[list[float]]]) -> Optional[list[float]]:
    """Union of axis-aligned bboxes; ignores None entries."""
    xs0, ys0, xs1, ys1 = [], [], [], []
    for b in bboxes:
        if not b or len(b) != 4:
            continue
        xs0.append(b[0]); ys0.append(b[1]); xs1.append(b[2]); ys1.append(b[3])
    if not xs0:
        return None
    return [min(xs0), min(ys0), max(xs1), max(ys1)]


def _section_path_str(section: dict | None) -> Optional[str]:
    if not section:
        return None
    path = section.get("path") or []
    return " > ".join(p for p in path if p) or None


def _common_meta(
    *,
    doc_id: str,
    doc_filename: Optional[str],
    doc_hash: Optional[str],
) -> dict:
    return {
        "doc_id": doc_id,
        "doc_filename": doc_filename,
        "doc_hash": doc_hash,
    }


# ---------------------------------------------------------------------------
# Section-aware text chunking
# ---------------------------------------------------------------------------
def _pack_paragraphs_into_windows(
    paragraphs: list[dict],
    max_chars: int,
    overlap_chars: int,
) -> list[list[dict]]:
    """Greedy pack a section's paragraphs into char-bounded windows.

    Each window is a list of paragraph dicts. Tail paragraphs of the
    previous window are repeated up to ``overlap_chars`` to provide
    context overlap (10–15%).
    """
    windows: list[list[dict]] = []
    cur: list[dict] = []
    cur_len = 0
    for p in paragraphs:
        plen = len(p.get("content", "")) + 2  # +separator
        if cur and cur_len + plen > max_chars:
            windows.append(cur)
            # Build overlap tail: last paragraphs adding up to overlap_chars
            tail: list[dict] = []
            tlen = 0
            for tp in reversed(cur):
                tplen = len(tp.get("content", "")) + 2
                if tlen + tplen > overlap_chars:
                    break
                tail.insert(0, tp)
                tlen += tplen
            cur = list(tail)
            cur_len = tlen
        cur.append(p)
        cur_len += plen
    if cur:
        windows.append(cur)
    return windows


def _hard_split_oversized(
    window: list[dict],
    max_chars: int,
    overlap_chars: int,
) -> list[list[dict]]:
    """If a window's combined text is far over budget (one giant paragraph),
    slice the joined text into char windows. Each slice is wrapped as a
    synthetic single-paragraph window inheriting page/bbox/section from the
    original first paragraph."""
    joined = "\n\n".join(p.get("content", "") for p in window)
    if len(joined) <= max_chars * HARD_SPLIT_HEADROOM:
        return [window]
    template = window[0]
    out: list[list[dict]] = []
    start = 0
    while start < len(joined):
        end = min(start + max_chars, len(joined))
        slice_p = dict(template)
        slice_p["content"] = joined[start:end]
        out.append([slice_p])
        if end >= len(joined):
            break
        start = end - overlap_chars
    return out


def chunk_paragraphs_by_section(
    paragraphs: list[dict],
    sections: list[dict],
    *,
    doc_id: str,
    doc_filename: Optional[str] = None,
    doc_hash: Optional[str] = None,
    chunk_tokens: int = CHUNK_TOKENS,
    chunk_overlap: int = CHUNK_OVERLAP,
) -> tuple[list[ChunkRecord], dict[str, str]]:
    """Build text chunks one section at a time.

    Returns ``(chunks, section_anchor_map)`` where ``section_anchor_map``
    maps ``section_id -> chunk_id`` of the *first* chunk emitted for
    that section. Tables / images use this map to set their
    ``parent_id``.

    Paragraphs that aren't attached to any section (e.g. page headers /
    footers) are grouped into a synthetic ``"_orphan"`` section so they
    still get indexed but never mix with real section text.
    """
    max_chars = chunk_tokens * 4
    overlap_chars = chunk_overlap * 4
    common = _common_meta(doc_id=doc_id, doc_filename=doc_filename, doc_hash=doc_hash)

    # paragraph index by id
    para_by_id = {p["id"]: p for p in paragraphs}
    section_by_id = {s["id"]: s for s in sections}

    # Group paragraphs by section in section reading order, paragraphs
    # within a section in reading order.
    grouped: dict[str, list[dict]] = {}
    section_order: list[str] = []
    for s in sorted(sections, key=lambda s: s.get("reading_order", 10**9)):
        sid = s["id"]
        sec_paras = [para_by_id[pid] for pid in s.get("paragraph_ids", []) if pid in para_by_id]
        # Skip pure-heading paragraphs from the body chunk; they're
        # already in section_path. But keep them if they have substantive
        # text (some PDFs put paragraph content under role=title).
        body = [
            p for p in sec_paras
            if p.get("role") not in ("pageHeader", "pageFooter")
            and p.get("content")
        ]
        if not body:
            continue
        grouped[sid] = sorted(body, key=lambda p: p.get("reading_order", 0))
        section_order.append(sid)

    # Orphan paragraphs (no section_id)
    orphans = [
        p for p in paragraphs
        if not p.get("section_id")
        and p.get("role") not in ("pageHeader", "pageFooter")
        and p.get("content")
    ]
    if orphans:
        grouped["_orphan"] = sorted(orphans, key=lambda p: p.get("reading_order", 0))
        section_order.append("_orphan")

    chunks: list[ChunkRecord] = []
    anchors: dict[str, str] = {}

    for sid in section_order:
        sec = section_by_id.get(sid)
        sec_path = _section_path_str(sec)
        sec_level = sec.get("level") if sec else None
        windows = _pack_paragraphs_into_windows(grouped[sid], max_chars, overlap_chars)
        # Hard-split any over-budget windows
        flat: list[list[dict]] = []
        for w in windows:
            flat.extend(_hard_split_oversized(w, max_chars, overlap_chars))

        for w in flat:
            content_parts: list[str] = []
            if sec_path:
                content_parts.append(f"[Section: {sec_path}]")
            content_parts.append("\n\n".join(p["content"] for p in w))
            content = "\n".join(content_parts)
            page = w[0].get("page", 0)
            # Union bboxes only of paragraphs on the chunk's primary page
            bbox = _union_bbox(p.get("bbox") for p in w if p.get("page") == page)
            reading_order = min(
                (p.get("reading_order", 10**9) for p in w),
                default=10**9,
            )
            chunk = ChunkRecord(
                **common,
                page=page,
                type="text",
                content=content,
                section_id=sid if sid != "_orphan" else None,
                section_path=sec_path,
                section_level=sec_level,
                element_id=None,
                bbox=bbox,
                reading_order=reading_order,
            )
            chunks.append(chunk)
            anchors.setdefault(sid, chunk.id)

    return chunks, anchors


# ---------------------------------------------------------------------------
# Tables (merged with nearby explanation)
# ---------------------------------------------------------------------------
def build_table_chunks(
    tables: list[dict],
    paragraphs: list[dict],
    *,
    doc_id: str,
    doc_filename: Optional[str] = None,
    doc_hash: Optional[str] = None,
    section_anchors: Optional[dict[str, str]] = None,
) -> list[ChunkRecord]:
    """One `ChunkRecord` per DI table, **never isolated** — the chunk
    body bundles the table's caption and its nearby paragraphs so the
    embedding has the explanation alongside the data."""
    common = _common_meta(doc_id=doc_id, doc_filename=doc_filename, doc_hash=doc_hash)
    para_by_id = {p["id"]: p for p in paragraphs}
    out: list[ChunkRecord] = []
    for t in tables:
        page = t.get("page", 0)
        sec_path = t.get("section_path")
        sid = t.get("section_id")
        caption = (t.get("caption") or "").strip()
        neighbor_text = _join_neighbors(t.get("neighbor_paragraph_ids") or [], para_by_id)

        header = f"[Table on page {page}"
        if caption:
            header += f" — {caption}"
        header += "]"

        body_parts: list[str] = [header]
        if sec_path:
            body_parts.append(f"Section: {sec_path}")
        if neighbor_text:
            body_parts.append(f"Context:\n{neighbor_text}")
        body_parts.append("Table:\n" + (t.get("content") or ""))
        content = "\n\n".join(body_parts)

        out.append(
            ChunkRecord(
                **common,
                page=page,
                type="table",
                content=content,
                caption=caption or None,
                section_id=sid,
                section_path=sec_path,
                element_id=f"/tables/{t['id'][1:]}" if t.get("id", "").startswith("t") else None,
                bbox=t.get("bbox"),
                reading_order=t.get("reading_order"),
                parent_id=(section_anchors or {}).get(sid),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Images (merged with caption + surrounding paragraphs)
# ---------------------------------------------------------------------------
def build_image_chunks(
    images: list[dict[str, Any]],
    paragraphs: Optional[list[dict]] = None,
    *,
    doc_id: str,
    doc_filename: Optional[str] = None,
    doc_hash: Optional[str] = None,
    section_anchors: Optional[dict[str, str]] = None,
) -> list[ChunkRecord]:
    """Convert image-extractor output into image chunks.

    Each chunk's body is laid out so a vector embedding sees:
      ``[Figure header] / Section / Caption / Surrounding text / Vision description``
    Image chunks point back at their section's anchor text chunk via
    ``parent_id`` so the UI can render related text alongside.
    """
    common = _common_meta(doc_id=doc_id, doc_filename=doc_filename, doc_hash=doc_hash)
    para_by_id = {p["id"]: p for p in (paragraphs or [])}
    chunks: list[ChunkRecord] = []
    for img in images:
        label = "Figure" if img.get("source") == "figure" else "Image"
        caption = (img.get("caption") or "").strip()
        sec_path = img.get("section_path")
        sid = img.get("section_id")
        page = img.get("page", 0)
        neighbor_text = _join_neighbors(img.get("neighbor_paragraph_ids") or [], para_by_id)
        description = img.get("description") or ""

        header = f"[{label} on page {page}"
        if caption:
            header += f" — {caption}"
        header += "]"

        body_parts: list[str] = [header]
        if sec_path:
            body_parts.append(f"Section: {sec_path}")
        if caption:
            body_parts.append(f"Caption: {caption}")
        if neighbor_text:
            body_parts.append(f"Surrounding text:\n{neighbor_text}")
        if description:
            body_parts.append(f"Description: {description}")
        content = "\n\n".join(body_parts)

        figure_id = img.get("figure_id")
        element_id = f"/figures/{figure_id[1:]}" if figure_id else None

        chunks.append(
            ChunkRecord(
                **common,
                page=page,
                type="image",
                content=content,
                image_url=img.get("image_url"),
                caption=caption or None,
                source=img.get("source"),
                section_id=sid,
                section_path=sec_path,
                element_id=element_id,
                bbox=img.get("bbox"),
                parent_id=(section_anchors or {}).get(sid),
            )
        )
    return chunks


def _join_neighbors(ids: list[str], para_by_id: dict[str, dict]) -> str:
    parts: list[str] = []
    for pid in ids:
        p = para_by_id.get(pid)
        if not p:
            continue
        txt = (p.get("content") or "").strip()
        if txt:
            parts.append(txt)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Legacy text chunker (per-page sliding window) — kept for callers that
# don't have rich paragraph data (smoke tests, stub fixtures).
# ---------------------------------------------------------------------------
def chunk_text_pages(
    page_chunks: list[dict],
    doc_id: str,
    *,
    chunk_tokens: int = CHUNK_TOKENS,
    chunk_overlap: int = CHUNK_OVERLAP,
    doc_filename: Optional[str] = None,
    doc_hash: Optional[str] = None,
) -> list[ChunkRecord]:
    """Sliding window over per-page text. Each chunk keeps its page number.

    Used as a fallback when the rich `paragraphs`/`sections` extraction
    is not available (e.g. tiny stub fixtures in the smoke tests).
    """
    common = _common_meta(doc_id=doc_id, doc_filename=doc_filename, doc_hash=doc_hash)
    out: list[ChunkRecord] = []
    max_chars = chunk_tokens * 4
    overlap = chunk_overlap * 4
    for pc in page_chunks:
        text: str = pc["content"]
        page: int = pc["page"]
        if len(text) <= max_chars:
            out.append(ChunkRecord(**common, page=page, type="text", content=text))
            continue
        start = 0
        while start < len(text):
            end = min(start + max_chars, len(text))
            out.append(
                ChunkRecord(**common, page=page, type="text", content=text[start:end])
            )
            if end >= len(text):
                break
            start = end - overlap
    return out


# ---------------------------------------------------------------------------
# Top-level assembly
# ---------------------------------------------------------------------------
def assemble_chunks(
    *,
    doc_id: str,
    text_pages: list[dict],
    tables: list[dict],
    image_chunks: list[ChunkRecord],
    paragraphs: Optional[list[dict]] = None,
    sections: Optional[list[dict]] = None,
    doc_filename: Optional[str] = None,
    doc_hash: Optional[str] = None,
    chunk_tokens: int = CHUNK_TOKENS,
    chunk_overlap: int = CHUNK_OVERLAP,
) -> list[ChunkRecord]:
    """Combine text / table / image chunks in a single ordered list.

    Prefers section-aware chunking when ``paragraphs`` and ``sections``
    are provided; otherwise falls back to the legacy per-page sliding
    window over ``text_pages``.

    Image chunks are passed in already-built (they require I/O — blob
    upload + vision description — that lives in ``doc_intelligence.py``).
    Their ``doc_id`` / ``doc_filename`` / ``doc_hash`` / ``parent_id``
    are normalized here to keep multi-doc retrieval consistent.
    """
    section_anchors: dict[str, str] = {}
    if paragraphs and sections:
        text_chunks, section_anchors = chunk_paragraphs_by_section(
            paragraphs,
            sections,
            doc_id=doc_id,
            doc_filename=doc_filename,
            doc_hash=doc_hash,
            chunk_tokens=chunk_tokens,
            chunk_overlap=chunk_overlap,
        )
    else:
        text_chunks = chunk_text_pages(
            text_pages,
            doc_id,
            chunk_tokens=chunk_tokens,
            chunk_overlap=chunk_overlap,
            doc_filename=doc_filename,
            doc_hash=doc_hash,
        )

    # Tables: rich path needs the dict shape produced by extract_pdf
    # (with id / bbox / section_id / neighbor_paragraph_ids); legacy
    # tables (just {content,page,type}) get a thin chunk.
    if tables and isinstance(tables[0], dict) and "id" in tables[0]:
        table_chunks = build_table_chunks(
            tables,
            paragraphs or [],
            doc_id=doc_id,
            doc_filename=doc_filename,
            doc_hash=doc_hash,
            section_anchors=section_anchors,
        )
    else:
        table_chunks = [
            ChunkRecord(
                doc_id=doc_id,
                doc_filename=doc_filename,
                doc_hash=doc_hash,
                page=t.get("page", 0),
                type="table",
                content=t.get("content", ""),
            )
            for t in tables
        ]

    # Backfill multi-doc + parent linkage onto pre-built image chunks.
    for ic in image_chunks:
        if doc_filename and not ic.doc_filename:
            ic.doc_filename = doc_filename
        if doc_hash and not ic.doc_hash:
            ic.doc_hash = doc_hash
        if ic.parent_id is None and ic.section_id and ic.section_id in section_anchors:
            ic.parent_id = section_anchors[ic.section_id]

    return text_chunks + table_chunks + image_chunks
