"""
src/models.py — Pydantic models for DocMind AI.

These models define the wire format for API requests/responses and the
shape of records persisted to Cosmos DB and Azure AI Search.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional, TypedDict
from uuid import uuid4

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid4())


# ---------------------------------------------------------------------------
# Document metadata
# ---------------------------------------------------------------------------
class StageEvent(BaseModel):
    """A single stage of the ingestion pipeline (for UI progress display)."""

    name: str
    status: Literal["pending", "running", "done", "failed"] = "pending"
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    detail: Optional[str] = None


class DocumentMeta(BaseModel):
    """Metadata for an uploaded document. Persisted in Cosmos `documents`."""

    id: str = Field(default_factory=_new_id)
    user_id: str = "anonymous"
    filename: str
    blob_name: str
    container: Optional[str] = None  # blob container holding the source PDF
    content_type: str = "application/pdf"
    size_bytes: int = 0
    status: Literal["pending", "processing", "indexed", "failed"] = "pending"
    total_pages: int = 0
    total_chunks: int = 0
    total_images: int = 0
    total_tables: int = 0
    error: Optional[str] = None
    stages: list[StageEvent] = Field(default_factory=list)
    created_at: str = Field(default_factory=_utcnow)
    indexed_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Chunk record (stored in AI Search)
# ---------------------------------------------------------------------------
class ChunkRecord(BaseModel):
    """A single retrievable chunk indexed in Azure AI Search.

    Carries enough layout / hierarchy metadata to support:
      * section-aware retrieval (`section_path`, `section_level`)
      * parent-child reconstruction (`parent_id`, `element_id`)
      * spatial grouping with figures / tables (`bbox`, `page`)
      * stable multi-document identification (`doc_id`, `doc_hash`,
        `doc_filename`)
      * deterministic ordering across a doc (`reading_order`)
    """

    id: str = Field(default_factory=_new_id)
    doc_id: str
    # Stable identifiers for the source PDF — make multi-doc filtering
    # and de-dup safe even if `doc_id` (a UUID) is regenerated.
    doc_filename: Optional[str] = None
    doc_hash: Optional[str] = None  # sha256 of the source bytes
    page: int = 0
    type: Literal["text", "table", "image"] = "text"
    content: str
    image_url: Optional[str] = None
    caption: Optional[str] = None
    # Provenance for image chunks: "figure" (DI) | "raster" (PyMuPDF) | None for text/table
    source: Optional[Literal["figure", "raster"]] = None
    # ---- Layout / hierarchy -----------------------------------------
    # Section path in document order, e.g. "1. Introduction > Background".
    section_path: Optional[str] = None
    section_id: Optional[str] = None
    section_level: Optional[int] = None
    # For table / image chunks: the id of the synthetic "parent" text
    # chunk in the same section (or the section anchor) so the UI can
    # show siblings. For text chunks: usually None.
    parent_id: Optional[str] = None
    # The DI element reference, e.g. "/tables/3" or "/figures/2" — useful
    # for traceability back to the raw analyse result.
    element_id: Optional[str] = None
    # Bounding box on `page`, in PDF points (x0, y0, x1, y1). Union of
    # all source paragraphs / table / figure regions on that page.
    bbox: Optional[list[float]] = None
    # Global reading order within the document (0-based).
    reading_order: Optional[int] = None
    embedding: Optional[list[float]] = None


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
class Source(BaseModel):
    chunk_id: str
    doc_id: str
    doc_filename: Optional[str] = None
    page: int
    type: str
    snippet: str
    image_url: Optional[str] = None
    caption: Optional[str] = None
    source: Optional[str] = None
    section_path: Optional[str] = None
    parent_id: Optional[str] = None
    score: Optional[float] = None


class ChatTurn(BaseModel):
    """A single user/assistant turn — persisted in Cosmos `sessions`."""

    id: str = Field(default_factory=_new_id)
    session_id: str
    user_id: str = "anonymous"
    role: Literal["user", "assistant"]
    content: str
    sources: list[Source] = Field(default_factory=list)
    created_at: str = Field(default_factory=_utcnow)


class ChatRequest(BaseModel):
    session_id: str = Field(default_factory=_new_id)
    message: str
    doc_ids: Optional[list[str]] = None  # restrict retrieval to specific docs


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    sources: list[Source] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Feedback & self-improvement
# ---------------------------------------------------------------------------
class FeedbackRequest(BaseModel):
    session_id: str
    turn_id: str
    rating: Literal["up", "down"]
    correction: Optional[str] = None  # free-text correction from user


class FeedbackChunkMeta(BaseModel):
    """Lightweight chunk metadata captured at feedback time so the
    learning loop can target penalties by modality / page without having
    to re-query the search index."""

    chunk_id: str
    type: str = "text"  # "text" | "table" | "image"
    page: int = 0


class FeedbackRecord(BaseModel):
    id: str = Field(default_factory=_new_id)
    session_id: str
    turn_id: str
    user_id: str = "anonymous"
    rating: Literal["up", "down"]
    correction: Optional[str] = None
    question: str = ""
    answer: str = ""
    chunk_ids: list[str] = Field(default_factory=list)
    # Per-chunk metadata (type, page) for the chunks cited in the answer.
    # Older feedback rows may not have this populated — code that uses it
    # MUST tolerate an empty list.
    chunk_meta: list[FeedbackChunkMeta] = Field(default_factory=list)
    created_at: str = Field(default_factory=_utcnow)


class LearnedRule(BaseModel):
    id: str = Field(default_factory=_new_id)
    category: str  # partition key — e.g. "general", "diagrams", "tables"
    rule: str
    evidence_count: int = 1
    created_at: str = Field(default_factory=_utcnow)
    updated_at: str = Field(default_factory=_utcnow)


class GoldenPair(BaseModel):
    """A high-quality Q&A pair used as few-shot example."""

    id: str = Field(default_factory=_new_id)
    topic: str  # partition key
    question: str
    answer: str
    chunk_ids: list[str] = Field(default_factory=list)
    confirmed_at: str = Field(default_factory=_utcnow)


class ChunkQuality(BaseModel):
    """Per-chunk retrieval quality score, used to re-rank search results."""

    id: str  # = chunk_id
    chunk_id: str
    times_retrieved: int = 0
    times_in_good_answer: int = 0
    times_in_bad_answer: int = 0
    quality_score: float = 0.5  # 0..1
    updated_at: str = Field(default_factory=_utcnow)


class IngestionTask(BaseModel):
    """Queued ingestion task — picked up by the worker."""

    id: str = Field(default_factory=_new_id)
    doc_id: str
    blob_name: str
    user_id: str = "anonymous"
    status: Literal["queued", "running", "done", "failed"] = "queued"
    error: Optional[str] = None
    created_at: str = Field(default_factory=_utcnow)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Document Intelligence extraction types
# ---------------------------------------------------------------------------
class ExtractedChunk(TypedDict):
    content: str
    page: int
    type: str  # "text" | "table"


class ExtractedParagraph(TypedDict):
    id: str               # "p<index>" — index in result.paragraphs
    content: str
    page: int
    bbox: Optional[list[float]]  # [x0,y0,x1,y1] in PDF points, or None
    role: Optional[str]   # DI role: "title" | "sectionHeading" | "pageHeader"...
    reading_order: int    # global index in document reading order
    section_id: Optional[str]


class ExtractedSection(TypedDict):
    id: str               # "s<index>"
    heading: str
    level: int            # 1 = root
    path: list[str]       # ancestor headings + own heading
    parent_id: Optional[str]
    paragraph_ids: list[str]
    table_ids: list[str]
    figure_ids: list[str]
    reading_order: int    # min reading_order of children


class ExtractedTable(TypedDict):
    id: str               # "t<index>"
    content: str          # markdown
    page: int
    bbox: Optional[list[float]]
    caption: str
    section_id: Optional[str]
    section_path: Optional[str]
    reading_order: int
    neighbor_paragraph_ids: list[str]


class ExtractedImage(TypedDict):
    page: int
    image_url: str
    blob_name: str
    description: str
    ext: str
    size_bytes: int
    source: str  # "figure" (DI) | "raster" (PyMuPDF)
    caption: str
    figure_id: Optional[str]            # "f<index>" for DI figures, else None
    bbox: Optional[list[float]]
    section_id: Optional[str]
    section_path: Optional[str]
    neighbor_paragraph_ids: list[str]
