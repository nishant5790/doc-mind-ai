"""
src/models.py — Pydantic models for DocMind AI.

These models define the wire format for API requests/responses and the
shape of records persisted to Cosmos DB and Azure AI Search.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional
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
    """A single retrievable chunk indexed in Azure AI Search."""

    id: str = Field(default_factory=_new_id)
    doc_id: str
    page: int = 0
    type: Literal["text", "table", "image"] = "text"
    content: str
    image_url: Optional[str] = None
    caption: Optional[str] = None
    # Provenance for image chunks: "figure" (DI) | "raster" (PyMuPDF) | None for text/table
    source: Optional[Literal["figure", "raster"]] = None
    embedding: Optional[list[float]] = None


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
class Source(BaseModel):
    chunk_id: str
    doc_id: str
    page: int
    type: str
    snippet: str
    image_url: Optional[str] = None
    caption: Optional[str] = None
    source: Optional[str] = None


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
