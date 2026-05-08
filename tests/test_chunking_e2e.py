"""
End-to-end component test for the section-aware chunking upgrade.

Runs each pipeline stage in isolation and prints a short diagnostic so
you can confirm the new metadata is flowing correctly:

    1. Document Intelligence  â€” paragraphs, sections, rich tables, figures_meta
    2. Chunking (offline)     â€” section_path, parent_id, bbox, doc_hash, reading_order
    3. Search                 â€” index schema picks up new fields, round-trip a doc
    4. Ingestion              â€” full pipeline on a real PDF

Run:    python tests/test_chunking_e2e.py
"""
from __future__ import annotations

import hashlib
import sys

import _bootstrap  # noqa: F401 â€” sys.path bootstrap

from src.blob_client import BlobService
from src.chunking import (
    CHUNK_OVERLAP,
    CHUNK_TOKENS,
    assemble_chunks,
    build_image_chunks,
    build_table_chunks,
    chunk_paragraphs_by_section,
)
from src.cosmos_client import create_state_service
from src.doc_intelligence import DocIntelService
from src.ingestion import IngestionPipeline
from src.models import ChunkRecord, DocumentMeta
from src.openai_client import OpenAIService
from src.search_client import SearchService


def _hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


def _show_chunk(c: ChunkRecord, max_chars: int = 180) -> None:
    print(
        f"  [{c.type:5}] page={c.page} order={c.reading_order} "
        f"sec={(c.section_path or '-')[:40]!r} "
        f"parent={c.parent_id or '-'} "
        f"elem={c.element_id or '-'} "
        f"bbox={'set' if c.bbox else '-'} "
        f"doc_filename={c.doc_filename!r} doc_hash={(c.doc_hash or '')[:8]}"
    )
    snippet = (c.content or "").replace("\n", " / ")[:max_chars]
    print(f"         content: {snippet}")


# ============================================================================
# 1. Document Intelligence  â€” extract_pdf returns rich data
# ============================================================================
def test_doc_intelligence(blob: BlobService, pdf_bytes: bytes) -> dict:
    _hr("1. Document Intelligence  â€” extract_pdf")

    name = "docmind-test/chunking-e2e.pdf"
    blob.upload(name, pdf_bytes, content_type="application/pdf")
    url = blob.get_url(name)

    di = DocIntelService()
    extracted = di.extract_pdf(url)

    print(f"  pages         : {extracted['pages']}")
    print(f"  text_chunks   : {len(extracted['text_chunks'])}")
    print(f"  paragraphs    : {len(extracted.get('paragraphs', []))}")
    print(f"  sections      : {len(extracted.get('sections', []))}")
    print(f"  tables        : {len(extracted['tables'])}")
    print(f"  figures (raw) : {len(extracted.get('figures', []))}")
    print(f"  figures_meta  : {len(extracted.get('figures_meta', []))}")

    paragraphs = extracted.get("paragraphs", [])
    sections = extracted.get("sections", [])
    tables = extracted["tables"]
    figures_meta = extracted.get("figures_meta", [])

    # ---- assertions on shape -------------------------------------------------
    assert paragraphs, "expected at least one paragraph from DI"
    p0 = paragraphs[0]
    for k in ("id", "content", "page", "bbox", "role", "reading_order", "section_id"):
        assert k in p0, f"paragraph missing '{k}'"
    print(f"\n  sample paragraph:")
    print(f"    id={p0['id']} page={p0['page']} role={p0['role']!r} "
          f"bbox={'set' if p0['bbox'] else '-'} section_id={p0['section_id']!r}")
    print(f"    text: {(p0['content'] or '')[:140]!r}")

    if sections:
        # show top-3 sections
        print(f"\n  sample sections (first 3):")
        for s in sections[:3]:
            for k in ("id", "heading", "level", "path", "parent_id",
                      "paragraph_ids", "table_ids", "figure_ids", "reading_order"):
                assert k in s, f"section missing '{k}'"
            print(
                f"    {s['id']} L{s['level']} "
                f"path={' > '.join(s['path'])[:60]!r} "
                f"paras={len(s['paragraph_ids'])} "
                f"tables={len(s['table_ids'])} "
                f"figs={len(s['figure_ids'])}"
            )
    else:
        print("  WARNING: DI returned no sections (PDF may have no headings)")

    if tables:
        t0 = tables[0]
        for k in ("id", "content", "page", "bbox", "caption",
                  "section_id", "section_path",
                  "reading_order", "neighbor_paragraph_ids"):
            assert k in t0, f"table missing '{k}'"
        print(f"\n  sample table:")
        print(f"    id={t0['id']} page={t0['page']} caption={t0['caption']!r}")
        print(f"    section_path={t0['section_path']!r}")
        print(f"    neighbors={t0['neighbor_paragraph_ids']}")
        print(f"    md (first 200 chars): {t0['content'][:200]!r}")

    if figures_meta:
        f0 = figures_meta[0]
        for k in ("id", "page", "bbox", "section_id",
                  "section_path", "neighbor_paragraph_ids", "reading_order"):
            assert k in f0, f"figures_meta missing '{k}'"
        print(f"\n  sample figure_meta:")
        print(f"    id={f0['id']} page={f0['page']} section_path={f0['section_path']!r}")
        print(f"    neighbors={f0['neighbor_paragraph_ids']}")

    print("\n  âœ” Document Intelligence extraction OK")
    return extracted


# ============================================================================
# 2. Chunking â€” section-aware, offline (no Azure calls)
# ============================================================================
def test_chunking(extracted: dict, doc_filename: str, doc_hash: str) -> list[ChunkRecord]:
    _hr("2. Chunking â€” section-aware, parent-child, multi-doc metadata")

    paragraphs = extracted.get("paragraphs", [])
    sections = extracted.get("sections", [])
    tables = extracted["tables"]

    # ---- 2a. Pure section-aware text chunker --------------------------------
    text_chunks, anchors = chunk_paragraphs_by_section(
        paragraphs,
        sections,
        doc_id="d-test",
        doc_filename=doc_filename,
        doc_hash=doc_hash,
        chunk_tokens=CHUNK_TOKENS,
        chunk_overlap=CHUNK_OVERLAP,
    )
    print(f"  text chunks emitted: {len(text_chunks)}")
    print(f"  section anchors    : {len(anchors)}")

    # Sanity assertions
    if text_chunks:
        for c in text_chunks:
            assert c.doc_filename == doc_filename
            assert c.doc_hash == doc_hash
            assert c.type == "text"
            # every chunk should be within ~ chunk_tokens*4 chars (+ small slack)
            assert len(c.content) <= CHUNK_TOKENS * 4 * 2, "chunk way over budget"
        # token-budget stats
        char_lens = [len(c.content) for c in text_chunks]
        avg = sum(char_lens) // len(char_lens)
        approx_avg_tokens = avg // 4
        print(f"  avg chunk chars    : {avg}  (~{approx_avg_tokens} tokens) "
              f"min={min(char_lens)} max={max(char_lens)}")
        # show first 2
        print("\n  first 2 text chunks:")
        for c in text_chunks[:2]:
            _show_chunk(c)

    # ---- 2b. Table chunks merge caption + neighbor text --------------------
    table_chunks = build_table_chunks(
        tables,
        paragraphs,
        doc_id="d-test",
        doc_filename=doc_filename,
        doc_hash=doc_hash,
        section_anchors=anchors,
    )
    print(f"\n  table chunks       : {len(table_chunks)}")
    for c in table_chunks[:2]:
        # tables in a section MUST link to their anchor
        if c.section_id and c.section_id in anchors:
            assert c.parent_id == anchors[c.section_id], (
                "table chunk missing parent_id link"
            )
        _show_chunk(c, max_chars=240)

    # ---- 2c. Image chunks (synthetic â€” no real images needed here) ---------
    fake_images = [
        {
            "page": 1,
            "image_url": "https://example/blob/page1.png",
            "blob_name": "x/y.png",
            "description": "Synthetic vision description for test.",
            "ext": "png",
            "size_bytes": 12345,
            "source": "figure",
            "caption": "Synthetic Caption",
            "figure_id": "f0",
            "bbox": [10, 20, 110, 220],
            "section_id": (sections[0]["id"] if sections else None),
            "section_path": (" > ".join(sections[0]["path"]) if sections else None),
            "neighbor_paragraph_ids": [paragraphs[0]["id"]] if paragraphs else [],
        }
    ]
    image_chunks = build_image_chunks(
        fake_images,
        paragraphs,
        doc_id="d-test",
        doc_filename=doc_filename,
        doc_hash=doc_hash,
        section_anchors=anchors,
    )
    print(f"\n  image chunks       : {len(image_chunks)}")
    for c in image_chunks:
        assert c.element_id == "/figures/0"
        if c.section_id and c.section_id in anchors:
            assert c.parent_id == anchors[c.section_id]
        assert c.image_url is not None
        _show_chunk(c, max_chars=240)

    # ---- 2d. assemble_chunks combines + backfills --------------------------
    bare_image = ChunkRecord(
        doc_id="d-test", page=1, type="image",
        content="bare image (no doc_filename, no parent)",
        image_url="https://example/bare.png",
        section_id=(sections[0]["id"] if sections else None),
    )
    all_chunks = assemble_chunks(
        doc_id="d-test",
        text_pages=extracted.get("text_chunks", []),
        tables=tables,
        image_chunks=[bare_image],
        paragraphs=paragraphs,
        sections=sections,
        doc_filename=doc_filename,
        doc_hash=doc_hash,
    )
    print(f"\n  assemble_chunks total: {len(all_chunks)}  "
          f"(text+table+1 backfilled image)")
    # confirm backfill happened
    assert bare_image.doc_filename == doc_filename, "doc_filename not backfilled"
    assert bare_image.doc_hash == doc_hash, "doc_hash not backfilled"
    if bare_image.section_id and bare_image.section_id in anchors:
        assert bare_image.parent_id == anchors[bare_image.section_id], (
            "parent_id not backfilled on image chunk"
        )
    print(f"  backfill OK: doc_filename={bare_image.doc_filename!r} "
          f"doc_hash={(bare_image.doc_hash or '')[:8]} "
          f"parent_id={bare_image.parent_id or '-'}")

    print("\n  âœ” Chunking metadata OK")
    return all_chunks


# ============================================================================
# 3. Search â€” schema accepts new fields and round-trips them
# ============================================================================
def test_search(chunks: list[ChunkRecord]) -> None:
    _hr("3. Search â€” index schema + round-trip of new metadata")

    search = SearchService()
    search.create_or_update_index()
    print("  âœ” create_or_update_index() succeeded (new fields registered)")

    if not chunks:
        print("  no chunks to round-trip; skipping")
        return

    # Embed the first 3 with a real OpenAI call so vector field is populated.
    sample = chunks[:3]
    ai = OpenAIService()
    vectors = ai.embed([c.content for c in sample])
    for c, v in zip(sample, vectors):
        c.embedding = v

    n = search.index_chunks(sample)
    print(f"  âœ” indexed {n}/{len(sample)} sample chunks")

    qv = ai.embed("overview")[0]
    sources = search.search("overview", qv, top_k=3, doc_ids=[sample[0].doc_id])
    print(f"  âœ” search returned {len(sources)} sources")
    for s in sources:
        print(
            f"    - {s.chunk_id[:8]} doc_filename={s.doc_filename!r} "
            f"page={s.page} type={s.type} "
            f"section_path={(s.section_path or '-')[:40]!r} "
            f"parent_id={s.parent_id or '-'}"
        )
        # Either filename should match the chunk we indexed, or at least be set
        assert s.doc_id == sample[0].doc_id

    # Cleanup the test doc id
    deleted = search.delete_document(sample[0].doc_id)
    print(f"  âœ” cleaned up {deleted} test chunks")


# ============================================================================
# 4. Ingestion â€” full pipeline end-to-end
# ============================================================================
def test_ingestion(blob: BlobService, pdf_path) -> None:
    _hr("4. Ingestion â€” full pipeline on real PDF")

    search = SearchService()
    search.create_or_update_index()
    cosmos = create_state_service()
    cosmos.ensure_containers()

    pipeline = IngestionPipeline(
        blob, DocIntelService(), OpenAIService(), search, cosmos
    )

    doc = DocumentMeta(
        user_id="chunk-e2e",
        filename=pdf_path.name,
        blob_name=f"chunk-e2e/{pdf_path.name}",
    )
    blob.upload(doc.blob_name, pdf_path.read_bytes(), content_type="application/pdf")
    cosmos.save_document(doc)

    doc = pipeline.process_pdf(doc)
    print(f"  status: {doc.status}  pages: {doc.total_pages}  "
          f"chunks: {doc.total_chunks}  tables: {doc.total_tables}  "
          f"images: {doc.total_images}")
    assert doc.status == "indexed"
    assert doc.total_chunks > 0

    # Pull a few chunks back out of Search to confirm metadata persisted.
    ai = OpenAIService()
    qv = ai.embed("introduction")[0]
    sources = search.search("introduction", qv, top_k=5, doc_ids=[doc.id])
    print(f"\n  retrieved {len(sources)} sources from indexed doc:")
    seen_section = False
    for s in sources:
        print(
            f"    - page={s.page} type={s.type} "
            f"section_path={(s.section_path or '-')[:50]!r} "
            f"doc_filename={s.doc_filename!r}"
        )
        if s.section_path:
            seen_section = True
        assert s.doc_filename == pdf_path.name, (
            f"doc_filename mismatch on retrieved source: {s.doc_filename!r}"
        )
    if not seen_section:
        print("  NOTE: no section_path on retrieved sources â€” PDF may lack headings")

    # Cleanup
    deleted = search.delete_document(doc.id)
    print(f"\n  âœ” cleaned up {deleted} chunks for test doc {doc.id[:8]}")


# ============================================================================
# Entry point
# ============================================================================
def main() -> int:
    pdf_path = _bootstrap.find_asset(
        "Multi_Agent_Research_System_Architecture.pdf",
        "sample.pdf",
    )
    print(f"Using PDF: {pdf_path}")
    pdf_bytes = pdf_path.read_bytes()
    doc_hash = hashlib.sha256(pdf_bytes).hexdigest()
    print(f"PDF size:  {len(pdf_bytes)} bytes")
    print(f"sha256:    {doc_hash}")

    blob = BlobService()
    blob.ensure_container()

    extracted = test_doc_intelligence(blob, pdf_bytes)
    chunks = test_chunking(extracted, doc_filename=pdf_path.name, doc_hash=doc_hash)
    test_search(chunks)
    test_ingestion(blob, pdf_path)

    _hr("ALL COMPONENT TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

