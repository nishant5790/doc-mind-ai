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
from typing import TYPE_CHECKING, Any, Optional, TypedDict

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
# DPI used when rasterizing a page region for a DI-detected figure.
FIGURE_RENDER_DPI = 200
# IoU threshold above which a PyMuPDF-extracted raster is considered a
# duplicate of a DI figure on the same page.
DEDUP_IOU = 0.4


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
    source: str  # "figure" (DI) | "raster" (PyMuPDF)
    caption: str


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
        # Note: `prebuilt-layout` returns `result.figures` automatically — there
        # is no `features=['figures']` add-on; passing it returns InvalidArgument.
        poller = self._client.begin_analyze_document(
            "prebuilt-layout",
            AnalyzeDocumentRequest(url_source=blob_url),
            output_content_format="markdown",
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
            "figures": list(result.figures or []),
        }

    # ------------------------------------------------------------------
    def extract_images(
        self,
        pdf_bytes: bytes,
        doc_id: str,
        blob: "BlobService",
        openai: Optional["OpenAIService"] = None,
        figures: Optional[list[Any]] = None,
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
        """
        import fitz  # PyMuPDF — local import keeps import cost off cold paths

        out: list[ExtractedImage] = []
        # page_number -> list[fitz.Rect] of regions already captured (for dedup)
        captured_rects: dict[int, list["fitz.Rect"]] = {}

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
