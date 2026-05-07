"""
app.py — DocMind AI FastAPI server.

Endpoints
---------
GET    /health                 — liveness/readiness
POST   /documents              — upload PDF; queues async ingestion
GET    /documents              — list user's docs
GET    /documents/{doc_id}     — single doc metadata
DELETE /documents/{doc_id}     — remove doc + all its chunks
POST   /chat                   — streaming Q&A (Server-Sent Events)
GET    /chat/{session_id}      — full session history
POST   /feedback               — 👍/👎 + optional correction
POST   /admin/learn            — trigger the self-improvement loop
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from src.auth import current_user
from src.blob_client import BlobService
from src.cosmos_client import create_state_service
from src.doc_intelligence import DocIntelService
from src.models import (
    ChatRequest,
    DocumentMeta,
    FeedbackRecord,
    FeedbackRequest,
    IngestionTask,
)
from src.openai_client import OpenAIService
from src.rag import RAGEngine
from src.redis_memory import create_chat_memory
from src.search_client import SearchService

import config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("docmind")

app = FastAPI(title="DocMind AI", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- service singletons ---
blob = BlobService()
# User-uploaded PDFs share the same container (the SAS token is
# scoped to it). They are namespaced under a `user-input/` key prefix.
user_blob = blob
search = SearchService()
doc_intel = DocIntelService()
openai_svc = OpenAIService()
cosmos = create_state_service()
chat_memory = create_chat_memory()
rag = RAGEngine(search, openai_svc, cosmos, memory=chat_memory)


@app.on_event("startup")
def _startup() -> None:
    for name, fn in (
        ("blob.ensure_container", blob.ensure_container),
        ("cosmos.ensure_containers", cosmos.ensure_containers),
        ("search.create_or_update_index", search.create_or_update_index),
    ):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            log.warning("Startup step %s failed (continuing): %s", name, exc)

    # Optional: wipe every persistent store on boot so each `docker compose up`
    # gives a clean slate (no stale documents, learning rules, chat history,
    # ingestion tasks, blobs, or search index). Controlled by FRESH_START env.
    import os

    fresh = os.environ.get("FRESH_START", "").strip().lower() in ("1", "true", "yes", "on")
    if fresh:
        log.warning("FRESH_START=true — wiping all persistent state on startup")
        try:
            wiped = cosmos.wipe_all()
            log.info("FRESH_START: cosmos/local-state wiped: %s", wiped)
        except Exception:
            log.exception("FRESH_START: cosmos wipe failed")
        try:
            chat_memory.flush_all()
            log.info("FRESH_START: chat memory flushed")
        except Exception:
            log.exception("FRESH_START: chat memory flush failed")
        try:
            search.wipe_index()
            log.info("FRESH_START: search index wiped and recreated")
        except Exception:
            log.exception("FRESH_START: search index wipe failed")
        try:
            n = user_blob.delete_all(prefix="")
            log.info("FRESH_START: deleted %d blobs", n)
        except Exception:
            log.exception("FRESH_START: blob wipe failed")

    log.info("DocMind AI started")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------
@app.post("/documents", status_code=202)
async def upload_document(
    file: UploadFile = File(...),
    user_id: str = Depends(current_user),
) -> DocumentMeta:
    if file.content_type not in ("application/pdf", "image/png", "image/jpeg"):
        raise HTTPException(400, "Only PDF / PNG / JPEG accepted")
    payload = await file.read()
    doc = DocumentMeta(
        user_id=user_id,
        filename=file.filename or "upload",
        blob_name=f"user-input/{user_id}/{file.filename}",
        container=user_blob.container,
        content_type=file.content_type,
        size_bytes=len(payload),
    )
    user_blob.upload(doc.blob_name, payload, content_type=file.content_type)
    cosmos.save_document(doc)
    cosmos.enqueue_task(IngestionTask(doc_id=doc.id, blob_name=doc.blob_name, user_id=user_id))
    return doc


@app.get("/documents")
def list_documents(user_id: str = Depends(current_user)) -> list[DocumentMeta]:
    return cosmos.list_documents(user_id=user_id)


@app.get("/documents/{doc_id}")
def get_document(doc_id: str, user_id: str = Depends(current_user)) -> DocumentMeta:
    doc = cosmos.get_document(doc_id, user_id=user_id)
    if not doc:
        raise HTTPException(404, "Not found")
    return doc


@app.delete("/documents/{doc_id}", status_code=204)
def delete_document(doc_id: str, user_id: str = Depends(current_user)) -> None:
    doc = cosmos.get_document(doc_id, user_id=user_id)
    if not doc:
        raise HTTPException(404, "Not found")
    # source PDF lives in whichever container it was uploaded to
    source_blob = user_blob if doc.container == user_blob.container else blob
    source_blob.delete(doc.blob_name)
    search.delete_document(doc_id)
    cosmos.delete_document(doc_id, user_id=user_id)


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
@app.post("/chat")
async def chat(req: ChatRequest, user_id: str = Depends(current_user)) -> StreamingResponse:
    async def event_stream():
        try:
            async for event in rag.stream_answer(
                req.message, session_id=req.session_id, user_id=user_id, doc_ids=req.doc_ids
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:
            log.exception("Chat error")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/chat/{session_id}")
def get_history(session_id: str, user_id: str = Depends(current_user)) -> list[dict]:
    return [t.model_dump() for t in chat_memory.get_history(session_id, limit=200)]


@app.get("/sessions")
def list_sessions(user_id: str = Depends(current_user)) -> list[dict]:
    """List the caller's chat sessions (most recent first)."""
    return chat_memory.list_sessions(user_id)


@app.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: str, user_id: str = Depends(current_user)) -> None:
    chat_memory.delete_session(session_id, user_id)


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------
@app.post("/feedback", status_code=204)
def submit_feedback(req: FeedbackRequest, user_id: str = Depends(current_user)) -> None:
    # Look up the original turn to capture question/answer/chunk_ids
    history = chat_memory.get_history(req.session_id, limit=200)
    by_id = {t.id: t for t in history}
    target = by_id.get(req.turn_id)
    if not target:
        raise HTTPException(404, "turn not found")
    # Find the immediately-preceding user turn for the question
    question = ""
    for prev in history:
        if prev.role == "user" and prev.created_at < target.created_at:
            question = prev.content
    cosmos.save_feedback(
        FeedbackRecord(
            session_id=req.session_id,
            turn_id=req.turn_id,
            user_id=user_id,
            rating=req.rating,
            correction=req.correction,
            question=question,
            answer=target.content,
            chunk_ids=[s.chunk_id for s in target.sources],
        )
    )


# ---------------------------------------------------------------------------
# Admin — trigger learning loop manually
# ---------------------------------------------------------------------------
@app.post("/admin/learn")
def trigger_learning(user_id: str = Depends(current_user)) -> dict:
    from src.learning import LearningLoop

    return LearningLoop(cosmos, openai_svc).run_once()


@app.get("/admin/rules")
def list_rules(user_id: str = Depends(current_user)) -> list[dict]:
    """All distilled guidelines learned from 👎 feedback corrections."""
    return [r.model_dump() for r in cosmos.list_rules(limit=200)]


@app.get("/admin/golden")
def list_golden(user_id: str = Depends(current_user)) -> list[dict]:
    """All Q&A pairs promoted from 👍 feedback (used as few-shot examples)."""
    return [g.model_dump() for g in cosmos.list_golden_pairs(limit=200)]


@app.get("/admin/feedback")
def list_feedback(user_id: str = Depends(current_user)) -> list[dict]:
    """Recent 👍/👎 events with optional corrections (raw signal)."""
    return [f.model_dump() for f in cosmos.list_feedback(limit=200)]


@app.delete("/admin/learning")
def wipe_learning(user_id: str = Depends(current_user)) -> dict:
    """Wipe all self-improvement state: feedback, rules, golden pairs, chunk quality."""
    counts = cosmos.clear_learning_state()
    return {"status": "ok", "deleted": counts}


# ---------------------------------------------------------------------------
# Admin — destructive cleanup endpoints
# ---------------------------------------------------------------------------
@app.delete("/admin/index")
def wipe_index(user_id: str = Depends(current_user)) -> dict:
    """Drop the AI Search index and recreate it empty.

    Also clears the caller's document metadata in Cosmos so the UI
    reflects reality (chunks are gone, docs are no longer searchable).
    """
    search.wipe_index()
    docs = cosmos.list_documents(user_id=user_id)
    for d in docs:
        try:
            cosmos.delete_document(d.id, user_id=user_id)
        except Exception as e:  # noqa: BLE001
            log.warning("cosmos delete_document failed for %s: %s", d.id, e)
    return {"status": "ok", "index_wiped": True, "docs_cleared": len(docs)}


@app.delete("/admin/blobs")
def wipe_blobs(
    prefix: Optional[str] = None,
    user_id: str = Depends(current_user),
) -> dict:
    """Delete blobs in the configured container.

    Defaults to deleting only this user's uploads (`user-input/{user_id}/`).
    Pass `?prefix=` (empty string) to wipe the whole container.
    """
    if prefix is None:
        prefix = f"user-input/{user_id}/"
    deleted = user_blob.delete_all(prefix=prefix)
    return {"status": "ok", "deleted": deleted, "prefix": prefix}


@app.delete("/admin/all")
def wipe_all(user_id: str = Depends(current_user)) -> dict:
    """Wipe AI Search index + all blobs + caller's document metadata."""
    search.wipe_index()
    deleted_blobs = user_blob.delete_all(prefix="")
    docs = cosmos.list_documents(user_id=user_id)
    for d in docs:
        try:
            cosmos.delete_document(d.id, user_id=user_id)
        except Exception as e:  # noqa: BLE001
            log.warning("cosmos delete_document failed for %s: %s", d.id, e)
    return {
        "status": "ok",
        "index_wiped": True,
        "blobs_deleted": deleted_blobs,
        "docs_cleared": len(docs),
    }
