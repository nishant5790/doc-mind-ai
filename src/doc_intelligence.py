"""
src/doc_intelligence.py — Azure Document Intelligence wrapper.

Uses the `prebuilt-layout` model to extract:
* per-page text (paragraphs / lines)
* tables (rendered as markdown)
* figures — visually detected regions with optional captions
  (returned by default in ``result.figures`` — no add-on feature required)

Image bytes are produced by a hybrid pipeline:
* DI tells us *where* figures are (works for vector charts / diagrams
  that have no underlying raster)
* PyMuPDF crops those regions from a rasterized page, AND also pulls
  any embedded raster XObjects DI may have missed
* a bbox-overlap dedup merges both sources so each visual artifact is
  uploaded once
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeResult, AnalyzeDocumentRequest
from azure.core.credentials import AzureKeyCredential

import config
from src.models import (
    ExtractedChunk,
    ExtractedImage,
    ExtractedParagraph,
    ExtractedSection,
    ExtractedTable,
)

if TYPE_CHECKING:  # avoid hard runtime deps for static typing
    from src.blob_client import BlobService
    from src.openai_client import OpenAIService

log = logging.getLogger(__name__)

# Resolved from config so callers importing this module still get the values.
MIN_IMAGE_BYTES = config.DOC_INTEL_MIN_IMAGE_BYTES
FIGURE_RENDER_DPI = config.DOC_INTEL_FIGURE_RENDER_DPI
DEDUP_IOU = config.DOC_INTEL_DEDUP_IOU

NEIGHBOR_PARAGRAPHS_BEFORE = config.DOC_INTEL_NEIGHBOR_PARAGRAPHS_BEFORE
NEIGHBOR_PARAGRAPHS_AFTER = config.DOC_INTEL_NEIGHBOR_PARAGRAPHS_AFTER


# Number of surrounding paragraphs (in same section, by reading order)
# attached to a table or figure as "nearby explanation" context.
NEIGHBOR_PARAGRAPHS_BEFORE = 2
NEIGHBOR_PARAGRAPHS_AFTER = 1


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
            * ``pages``        — number of pages
            * ``text_chunks``  — list[ExtractedChunk]   one per page (legacy)
            * ``tables``       — list[ExtractedTable]   rich, with bbox/section
            * ``paragraphs``   — list[ExtractedParagraph] in reading order
            * ``sections``     — list[ExtractedSection]   with parent/child ids
            * ``figures``      — raw DI figure objects (consumed by extract_images)
            * ``figures_meta`` — list[dict] section/neighbor lookup keyed by figure_id
        """
        log.info("Document Intelligence analysing %s", blob_url[:80])
        # Note: `prebuilt-layout` returns `result.figures` automatically — there
        # is no `features=['figures']` add-on; passing it returns InvalidArgument.
        poller = self._client.begin_analyze_document(
            "prebuilt-layout",
            AnalyzeDocumentRequest(url_source=blob_url),
            output_content_format="markdown",
        )
        result: AnalyzeResult = poller.result()

        # ---- per-page raw text (legacy text_chunks) ------------------
        text_chunks: list[ExtractedChunk] = []
        for page in result.pages or []:
            lines = [ln.content for ln in (page.lines or []) if ln.content]
            page_text = "\n".join(lines).strip()
            if page_text:
                text_chunks.append(
                    {"content": page_text, "page": page.page_number, "type": "text"}
                )

        # ---- paragraphs (reading order) ------------------------------
        paragraphs: list[ExtractedParagraph] = []
        for i, p in enumerate(result.paragraphs or []):
            page_no, bbox = self._region_to_page_bbox(getattr(p, "bounding_regions", None))
            paragraphs.append(
                {
                    "id": f"p{i}",
                    "content": (p.content or "").strip(),
                    "page": page_no,
                    "bbox": bbox,
                    "role": getattr(p, "role", None),
                    "reading_order": i,
                    "section_id": None,  # filled below
                }
            )

        # ---- sections + parent/child wiring --------------------------
        # DI returns sections with `elements` like "/paragraphs/12",
        # "/sections/3", "/tables/0", "/figures/1" — we resolve these
        # into typed id lists and a parent map.
        raw_sections = list(result.sections or [])
        # First pass: collect element refs and direct children sections
        section_records: list[dict] = []
        para_to_section: dict[int, int] = {}      # paragraph index -> section index
        table_to_section: dict[int, int] = {}
        figure_to_section: dict[int, int] = {}
        child_to_parent: dict[int, int] = {}      # section index -> parent section index
        for s_idx, sec in enumerate(raw_sections):
            paragraph_idxs: list[int] = []
            table_idxs: list[int] = []
            figure_idxs: list[int] = []
            for elem in getattr(sec, "elements", None) or []:
                kind, idx = self._parse_element_ref(elem)
                if kind == "paragraphs" and idx is not None:
                    paragraph_idxs.append(idx)
                    para_to_section.setdefault(idx, s_idx)
                elif kind == "tables" and idx is not None:
                    table_idxs.append(idx)
                    table_to_section.setdefault(idx, s_idx)
                elif kind == "figures" and idx is not None:
                    figure_idxs.append(idx)
                    figure_to_section.setdefault(idx, s_idx)
                elif kind == "sections" and idx is not None:
                    child_to_parent.setdefault(idx, s_idx)
            section_records.append(
                {
                    "paragraph_idxs": paragraph_idxs,
                    "table_idxs": table_idxs,
                    "figure_idxs": figure_idxs,
                }
            )

        # Compute level + path for each section (BFS from roots).
        levels: dict[int, int] = {}
        paths: dict[int, list[str]] = {}

        def _heading_for(s_idx: int) -> str:
            rec = section_records[s_idx]
            for pi in rec["paragraph_idxs"]:
                if 0 <= pi < len(paragraphs):
                    role = paragraphs[pi]["role"]
                    if role in ("title", "sectionHeading"):
                        return paragraphs[pi]["content"]
            # Fall back to first paragraph's content trimmed
            if rec["paragraph_idxs"]:
                pi = rec["paragraph_idxs"][0]
                if 0 <= pi < len(paragraphs):
                    return paragraphs[pi]["content"][:80]
            return f"Section {s_idx}"

        for s_idx in range(len(raw_sections)):
            # Walk up parents
            chain = [s_idx]
            cur = s_idx
            seen = {s_idx}
            while cur in child_to_parent:
                parent = child_to_parent[cur]
                if parent in seen:
                    break  # defensive: avoid cycles
                chain.append(parent)
                seen.add(parent)
                cur = parent
            chain.reverse()  # root -> self
            levels[s_idx] = len(chain)
            paths[s_idx] = [_heading_for(i) for i in chain]

        # Build sections list and back-fill paragraph.section_id
        sections: list[ExtractedSection] = []
        for s_idx, rec in enumerate(section_records):
            sec_id = f"s{s_idx}"
            for pi in rec["paragraph_idxs"]:
                if 0 <= pi < len(paragraphs):
                    paragraphs[pi]["section_id"] = sec_id
            min_order = min(
                (paragraphs[pi]["reading_order"] for pi in rec["paragraph_idxs"]
                 if 0 <= pi < len(paragraphs)),
                default=10**9,
            )
            sections.append(
                {
                    "id": sec_id,
                    "heading": paths[s_idx][-1] if paths[s_idx] else f"Section {s_idx}",
                    "level": levels.get(s_idx, 1),
                    "path": paths[s_idx],
                    "parent_id": (
                        f"s{child_to_parent[s_idx]}" if s_idx in child_to_parent else None
                    ),
                    "paragraph_ids": [f"p{pi}" for pi in rec["paragraph_idxs"]],
                    "table_ids": [f"t{ti}" for ti in rec["table_idxs"]],
                    "figure_ids": [f"f{fi}" for fi in rec["figure_idxs"]],
                    "reading_order": min_order,
                }
            )

        # ---- tables (rich) -------------------------------------------
        tables: list[ExtractedTable] = []
        for t_idx, table in enumerate(result.tables or []):
            md = self._table_to_markdown(table)
            page_no, bbox = self._region_to_page_bbox(
                getattr(table, "bounding_regions", None)
            )
            caption = ""
            try:
                if getattr(table, "caption", None) and table.caption.content:
                    caption = table.caption.content.strip()
            except AttributeError:
                caption = ""
            sec_idx = table_to_section.get(t_idx)
            sec_id = f"s{sec_idx}" if sec_idx is not None else None
            sec_path = " > ".join(paths[sec_idx]) if sec_idx is not None else None
            neighbors = self._neighbor_paragraphs_for_element(
                element_kind="table",
                bbox=bbox,
                page=page_no,
                section_idx=sec_idx,
                section_records=section_records,
                paragraphs=paragraphs,
            )
            tables.append(
                {
                    "id": f"t{t_idx}",
                    "content": md,
                    "page": page_no,
                    "bbox": bbox,
                    "caption": caption,
                    "section_id": sec_id,
                    "section_path": sec_path,
                    "reading_order": neighbors["reading_order_anchor"],
                    "neighbor_paragraph_ids": neighbors["ids"],
                }
            )

        # ---- figures_meta (consumed by extract_images) ---------------
        figures_meta: list[dict] = []
        for f_idx, fig in enumerate(result.figures or []):
            page_no, bbox = self._region_to_page_bbox(
                getattr(fig, "bounding_regions", None)
            )
            sec_idx = figure_to_section.get(f_idx)
            sec_id = f"s{sec_idx}" if sec_idx is not None else None
            sec_path = " > ".join(paths[sec_idx]) if sec_idx is not None else None
            neighbors = self._neighbor_paragraphs_for_element(
                element_kind="figure",
                bbox=bbox,
                page=page_no,
                section_idx=sec_idx,
                section_records=section_records,
                paragraphs=paragraphs,
            )
            figures_meta.append(
                {
                    "id": f"f{f_idx}",
                    "page": page_no,
                    "bbox": bbox,
                    "section_id": sec_id,
                    "section_path": sec_path,
                    "neighbor_paragraph_ids": neighbors["ids"],
                    "reading_order": neighbors["reading_order_anchor"],
                }
            )

        return {
            "pages": len(result.pages or []),
            "text_chunks": text_chunks,
            "tables": tables,
            "paragraphs": paragraphs,
            "sections": sections,
            "figures": list(result.figures or []),
            "figures_meta": figures_meta,
        }

    # ------------------------------------------------------------------
    def extract_images(
        self,
        pdf_bytes: bytes,
        doc_id: str,
        blob: "BlobService",
        openai: Optional["OpenAIService"] = None,
        figures: Optional[list[Any]] = None,
        figures_meta: Optional[list[dict]] = None,
        min_image_bytes: int = MIN_IMAGE_BYTES,
        render_dpi: int = FIGURE_RENDER_DPI,
    ) -> list[ExtractedImage]:
        """Hybrid image extraction: DI figures + PyMuPDF rasters.

        Strategy
        --------
        1. For every figure detected by Document Intelligence
           (``features=['figures']``), crop its bounding region from a
           rasterized page using PyMuPDF. This captures vector charts,
           composite diagrams, and screenshots drawn as paths — things
           ``page.get_images()`` cannot see.
        2. Then iterate embedded raster XObjects via PyMuPDF and add any
           that don't significantly overlap a figure already captured
           on the same page (IoU < ``DEDUP_IOU``).
        3. Each kept image is uploaded to Blob and (optionally) verbalized
           by GPT-4o vision. The figure's caption — when present — is
           passed in as a hint to improve description quality.

        Pass ``figures`` from :py:meth:`extract_pdf` (the ``"figures"``
        key) to enable step 1. If omitted, falls back to PyMuPDF only.
        Pass ``figures_meta`` from :py:meth:`extract_pdf` (the
        ``"figures_meta"`` key) to attach section / neighbor metadata
        onto each emitted figure-source image (used by chunking).
        """
        import fitz  # PyMuPDF — local import keeps import cost off cold paths

        out: list[ExtractedImage] = []
        # page_number -> list[fitz.Rect] of regions already captured (for dedup)
        captured_rects: dict[int, list["fitz.Rect"]] = {}
        # Quick lookup: figure_id "f<idx>" -> meta dict
        meta_by_id: dict[str, dict] = {m["id"]: m for m in (figures_meta or [])}

        pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            # ---- 1. DI figures: crop rendered page region ----------------
            for fi, fig in enumerate(figures or []):
                caption = ""
                try:
                    if fig.caption and fig.caption.content:
                        caption = fig.caption.content.strip()
                except AttributeError:
                    caption = ""

                fig_id = f"f{fi}"
                fmeta = meta_by_id.get(fig_id, {})

                for br_idx, br in enumerate(fig.bounding_regions or []):
                    page_num = br.page_number
                    if page_num < 1 or page_num > pdf_doc.page_count:
                        continue
                    page = pdf_doc[page_num - 1]
                    rect = self._polygon_to_rect(br.polygon, page, fitz)
                    if rect is None or rect.is_empty:
                        continue

                    try:
                        pix = page.get_pixmap(clip=rect, dpi=render_dpi)
                        image_bytes = pix.tobytes("png")
                    except Exception as e:  # noqa: BLE001
                        log.warning("Failed to render figure %s on page %s: %s", fi, page_num, e)
                        continue

                    if len(image_bytes) < min_image_bytes:
                        continue

                    blob_name = f"{doc_id}/figures/page{page_num}_fig{fi}_{br_idx}.png"
                    blob.upload(blob_name, image_bytes, content_type="image/png")
                    image_url = blob.get_url(blob_name)

                    description = self._describe(openai, image_url, caption, blob_name)
                    out.append(
                        {
                            "page": page_num,
                            "image_url": image_url,
                            "blob_name": blob_name,
                            "description": description,
                            "ext": "png",
                            "size_bytes": len(image_bytes),
                            "source": "figure",
                            "caption": caption,
                            "figure_id": fig_id,
                            "bbox": fmeta.get("bbox") or [
                                rect.x0, rect.y0, rect.x1, rect.y1,
                            ],
                            "section_id": fmeta.get("section_id"),
                            "section_path": fmeta.get("section_path"),
                            "neighbor_paragraph_ids": list(
                                fmeta.get("neighbor_paragraph_ids") or []
                            ),
                        }
                    )
                    captured_rects.setdefault(page_num, []).append(rect)

            # ---- 2. PyMuPDF raster XObjects (dedup against figures) ------
            for page_num, page in enumerate(pdf_doc, start=1):
                page_captured = captured_rects.get(page_num, [])
                for idx, img_info in enumerate(page.get_images(full=True)):
                    xref = img_info[0]

                    # Where on the page is this raster placed?
                    try:
                        placements = page.get_image_rects(xref) or []
                    except Exception:  # noqa: BLE001
                        placements = []

                    if placements and page_captured:
                        # Skip if any placement overlaps a DI-captured figure
                        if any(
                            self._iou(p, q) >= DEDUP_IOU
                            for p in placements
                            for q in page_captured
                        ):
                            continue

                    base = pdf_doc.extract_image(xref)
                    image_bytes = base["image"]
                    ext = base["ext"]
                    if len(image_bytes) < min_image_bytes:
                        continue

                    blob_name = f"{doc_id}/images/page{page_num}_img{idx}.{ext}"
                    blob.upload(blob_name, image_bytes, content_type=f"image/{ext}")
                    image_url = blob.get_url(blob_name)

                    description = self._describe(openai, image_url, "", blob_name)
                    raster_bbox = None
                    if placements:
                        r = placements[0]
                        raster_bbox = [r.x0, r.y0, r.x1, r.y1]
                    out.append(
                        {
                            "page": page_num,
                            "image_url": image_url,
                            "blob_name": blob_name,
                            "description": description,
                            "ext": ext,
                            "size_bytes": len(image_bytes),
                            "source": "raster",
                            "caption": "",
                            "figure_id": None,
                            "bbox": raster_bbox,
                            "section_id": None,
                            "section_path": None,
                            "neighbor_paragraph_ids": [],
                        }
                    )
                    if placements:
                        page_captured.extend(placements)
                        captured_rects[page_num] = page_captured
        finally:
            pdf_doc.close()
        return out

    # ------------------------------------------------------------------
    @staticmethod
    def _describe(
        openai: Optional["OpenAIService"],
        image_url: str,
        caption: str,
        blob_name: str,
    ) -> str:
        """Run GPT-4o vision with an optional caption hint."""
        if openai is None:
            return caption or "[image]"
        prompt = None
        if caption:
            prompt = (
                f"This figure has the caption: \"{caption}\".\n"
                "Describe the image in detail for a knowledge base, using the "
                "caption as ground truth. If it is a chart, extract data points. "
                "If it is a diagram, describe components and relationships. "
                "If it contains text, transcribe it exactly. Be concise but complete."
            )
        try:
            desc = openai.describe_image(image_url, prompt=prompt) if prompt else openai.describe_image(image_url)
        except Exception as e:  # noqa: BLE001
            log.warning("Vision failed for %s: %s", blob_name, e)
            return caption or "[image]"
        if caption and caption.lower() not in desc.lower():
            desc = f"{caption}\n\n{desc}"
        return desc

    # ------------------------------------------------------------------
    @staticmethod
    def _polygon_to_rect(polygon, page, fitz_mod):
        """Convert a DI bounding polygon (inches) to a fitz.Rect (points).

        Document Intelligence returns polygons as a flat ``[x1, y1, x2, y2, ...]``
        list in inches by default. PyMuPDF uses points (72 per inch) with the
        origin at the top-left of the page — same orientation as DI.
        """
        if not polygon or len(polygon) < 4:
            return None
        # polygon may be list[float] or list[Point]; normalize.
        if hasattr(polygon[0], "x"):
            xs = [float(p.x) for p in polygon]
            ys = [float(p.y) for p in polygon]
        else:
            xs = [float(v) for v in polygon[0::2]]
            ys = [float(v) for v in polygon[1::2]]
        x0, x1 = min(xs) * 72.0, max(xs) * 72.0
        y0, y1 = min(ys) * 72.0, max(ys) * 72.0
        # Clip to actual page size to avoid get_pixmap errors.
        page_rect = page.rect
        x0 = max(x0, page_rect.x0)
        y0 = max(y0, page_rect.y0)
        x1 = min(x1, page_rect.x1)
        y1 = min(y1, page_rect.y1)
        if x1 <= x0 or y1 <= y0:
            return None
        return fitz_mod.Rect(x0, y0, x1, y1)

    # ------------------------------------------------------------------
    @staticmethod
    def _iou(a, b) -> float:
        """Intersection-over-Union of two fitz.Rect objects."""
        ix0 = max(a.x0, b.x0)
        iy0 = max(a.y0, b.y0)
        ix1 = min(a.x1, b.x1)
        iy1 = min(a.y1, b.y1)
        iw = max(0.0, ix1 - ix0)
        ih = max(0.0, iy1 - iy0)
        inter = iw * ih
        if inter <= 0:
            return 0.0
        area_a = max(0.0, a.x1 - a.x0) * max(0.0, a.y1 - a.y0)
        area_b = max(0.0, b.x1 - b.x0) * max(0.0, b.y1 - b.y0)
        union = area_a + area_b - inter
        return inter / union if union > 0 else 0.0

    # ------------------------------------------------------------------
    @staticmethod
    def _region_to_page_bbox(bounding_regions) -> tuple[int, Optional[list[float]]]:
        """Return (page_number, bbox-in-points) from a DI bounding_regions list.

        Falls back to (0, None) when no region is present.
        """
        if not bounding_regions:
            return 0, None
        br = bounding_regions[0]
        page_no = getattr(br, "page_number", 0) or 0
        polygon = getattr(br, "polygon", None) or []
        if not polygon:
            return page_no, None
        if hasattr(polygon[0], "x"):
            xs = [float(p.x) for p in polygon]
            ys = [float(p.y) for p in polygon]
        else:
            xs = [float(v) for v in polygon[0::2]]
            ys = [float(v) for v in polygon[1::2]]
        if not xs or not ys:
            return page_no, None
        # DI returns inches by default — convert to PDF points (72 / inch).
        x0, x1 = min(xs) * 72.0, max(xs) * 72.0
        y0, y1 = min(ys) * 72.0, max(ys) * 72.0
        return page_no, [x0, y0, x1, y1]

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_element_ref(ref: str) -> tuple[Optional[str], Optional[int]]:
        """Parse DI element refs like '/paragraphs/12' -> ('paragraphs', 12)."""
        if not isinstance(ref, str) or not ref.startswith("/"):
            return None, None
        parts = ref.strip("/").split("/")
        if len(parts) < 2:
            return None, None
        kind = parts[0]
        try:
            idx = int(parts[1])
        except (TypeError, ValueError):
            return kind, None
        return kind, idx

    # ------------------------------------------------------------------
    @staticmethod
    def _neighbor_paragraphs_for_element(
        *,
        element_kind: str,
        bbox: Optional[list[float]],
        page: int,
        section_idx: Optional[int],
        section_records: list[dict],
        paragraphs: list,
    ) -> dict:
        """Find paragraphs that contextualize a table or figure.

        Strategy: look only inside the same DI section. Take the
        ``NEIGHBOR_PARAGRAPHS_BEFORE`` paragraphs immediately preceding
        the element's reading-order position and the
        ``NEIGHBOR_PARAGRAPHS_AFTER`` immediately following — these are
        what humans treat as the "caption / explanation" region.

        Returns a dict ``{"ids": [...], "reading_order_anchor": int}``.
        """
        if section_idx is None or not (0 <= section_idx < len(section_records)):
            return {"ids": [], "reading_order_anchor": 10**9}
        para_idxs = section_records[section_idx]["paragraph_idxs"]
        if not para_idxs:
            return {"ids": [], "reading_order_anchor": 10**9}

        # Anchor = approximate insertion point of this element in the
        # section's paragraph list. DI does not give a paragraph-relative
        # offset for tables/figures, so we use the *closest paragraph by
        # vertical position on the same page* as the anchor.
        anchor_pos = len(para_idxs)  # default: element after section text
        if bbox is not None:
            best_dist = None
            for pos, pi in enumerate(para_idxs):
                if 0 <= pi < len(paragraphs):
                    p = paragraphs[pi]
                    if p["page"] != page or p["bbox"] is None:
                        continue
                    # vertical distance of paragraph midline to element top
                    p_mid_y = (p["bbox"][1] + p["bbox"][3]) / 2.0
                    dist = abs(p_mid_y - bbox[1])
                    if best_dist is None or dist < best_dist:
                        best_dist = dist
                        anchor_pos = pos + 1  # element comes after this para
        before = para_idxs[max(0, anchor_pos - NEIGHBOR_PARAGRAPHS_BEFORE):anchor_pos]
        after = para_idxs[anchor_pos:anchor_pos + NEIGHBOR_PARAGRAPHS_AFTER]
        ids = [f"p{pi}" for pi in (before + after)]
        # reading_order anchor = order of last "before" paragraph + 0.5,
        # so element sorts naturally between text chunks.
        anchor_order: int = 10**9
        if before and 0 <= before[-1] < len(paragraphs):
            anchor_order = paragraphs[before[-1]]["reading_order"] + 1
        elif after and 0 <= after[0] < len(paragraphs):
            anchor_order = max(0, paragraphs[after[0]]["reading_order"] - 1)
        return {"ids": ids, "reading_order_anchor": anchor_order}

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
