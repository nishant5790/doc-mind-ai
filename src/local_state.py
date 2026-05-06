"""
src/local_state.py — File-backed fallback for CosmosService.

Provides the same public API as `src.cosmos_client.CosmosService` but
persists state to a single JSON file on local disk. Use this when no
Cosmos DB account is available (local dev / demo). Switch to the real
Cosmos backend by setting COSMOS_ENDPOINT and COSMOS_KEY in the
environment.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.models import (
    ChatTurn,
    ChunkQuality,
    DocumentMeta,
    FeedbackRecord,
    GoldenPair,
    IngestionTask,
    LearnedRule,
)

log = logging.getLogger(__name__)


_DEFAULT_PATH = os.environ.get("LOCAL_STATE_PATH", "/app/data/state.json")


class LocalStateService:
    """In-process, file-backed implementation of the CosmosService surface."""

    def __init__(self, path: str = _DEFAULT_PATH) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, dict]] = {
            "sessions": {},
            "documents": {},
            "feedback": {},
            "learned_rules": {},
            "golden_pairs": {},
            "chunk_quality": {},
            "ingestion_tasks": {},
        }
        self._load()
        log.warning(
            "Using LocalStateService (file-backed) at %s — Cosmos DB is NOT configured",
            self._path,
        )

    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            if self._path.exists():
                with self._path.open("r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    for k in self._data:
                        if isinstance(loaded.get(k), dict):
                            self._data[k] = loaded[k]
        except Exception:
            log.exception("Failed to load local state from %s; starting empty", self._path)

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, default=str)
            tmp.replace(self._path)
        except Exception:
            log.exception("Failed to persist local state to %s", self._path)

    def _upsert(self, container: str, item: dict) -> None:
        with self._lock:
            # Reload first so we merge with concurrent writes from other procs.
            self._load()
            self._data[container][item["id"]] = item
            self._save()

    def _all(self, container: str) -> list[dict]:
        with self._lock:
            # Always reload so we see writes from other processes (api/worker).
            self._load()
            return list(self._data[container].values())

    # ------------------------------------------------------------------
    def ensure_containers(self) -> None:  # no-op
        log.info("LocalStateService ready: %s", list(self._data.keys()))

    # ===== sessions =====
    def save_turn(self, turn: ChatTurn) -> None:
        self._upsert("sessions", turn.model_dump())

    def get_history(self, session_id: str, limit: int = 20) -> list[ChatTurn]:
        items = [i for i in self._all("sessions") if i.get("session_id") == session_id]
        items.sort(key=lambda i: i.get("created_at", ""))
        return [ChatTurn(**i) for i in items[:limit]]

    # ===== documents =====
    def save_document(self, doc: DocumentMeta) -> None:
        self._upsert("documents", doc.model_dump())

    def get_document(self, doc_id: str, user_id: str = "anonymous") -> Optional[DocumentMeta]:
        with self._lock:
            self._load()
            item = self._data["documents"].get(doc_id)
        if not item or item.get("user_id") != user_id:
            return None
        return DocumentMeta(**item)

    def list_documents(self, user_id: str = "anonymous") -> list[DocumentMeta]:
        items = [i for i in self._all("documents") if i.get("user_id") == user_id]
        return [DocumentMeta(**i) for i in items]

    def delete_document(self, doc_id: str, user_id: str = "anonymous") -> None:
        with self._lock:
            self._load()
            item = self._data["documents"].get(doc_id)
            if item and item.get("user_id") == user_id:
                self._data["documents"].pop(doc_id, None)
                self._save()

    # ===== feedback =====
    def save_feedback(self, fb: FeedbackRecord) -> None:
        self._upsert("feedback", fb.model_dump())

    def list_feedback(self, limit: int = 200) -> list[FeedbackRecord]:
        items = self._all("feedback")
        items.sort(key=lambda i: i.get("created_at", ""), reverse=True)
        return [FeedbackRecord(**i) for i in items[:limit]]

    # ===== learned rules =====
    def save_rule(self, rule: LearnedRule) -> None:
        self._upsert("learned_rules", rule.model_dump())

    def get_rules(self, category: str = "general", top: int = 5) -> list[LearnedRule]:
        items = [i for i in self._all("learned_rules") if i.get("category") == category]
        items.sort(key=lambda i: i.get("evidence_count", 0), reverse=True)
        return [LearnedRule(**i) for i in items[:top]]

    def list_rules(self, limit: int = 100) -> list[LearnedRule]:
        items = self._all("learned_rules")
        items.sort(key=lambda i: i.get("updated_at", ""), reverse=True)
        return [LearnedRule(**i) for i in items[:limit]]

    # ===== golden pairs =====
    def save_golden(self, gp: GoldenPair) -> None:
        self._upsert("golden_pairs", gp.model_dump())

    def get_golden_pairs(self, topic: str = "general", top: int = 3) -> list[GoldenPair]:
        items = [i for i in self._all("golden_pairs") if i.get("topic") == topic]
        items.sort(key=lambda i: i.get("confirmed_at", ""), reverse=True)
        return [GoldenPair(**i) for i in items[:top]]

    def list_golden_pairs(self, limit: int = 100) -> list[GoldenPair]:
        items = self._all("golden_pairs")
        items.sort(key=lambda i: i.get("confirmed_at", ""), reverse=True)
        return [GoldenPair(**i) for i in items[:limit]]

    # ===== chunk quality =====
    def update_chunk_quality(
        self, chunk_id: str, *, retrieved: bool = False, good: bool = False, bad: bool = False
    ) -> None:
        with self._lock:
            self._load()
            item = self._data["chunk_quality"].get(chunk_id)
            cq = ChunkQuality(**item) if item else ChunkQuality(id=chunk_id, chunk_id=chunk_id)
            if retrieved:
                cq.times_retrieved += 1
            if good:
                cq.times_in_good_answer += 1
            if bad:
                cq.times_in_bad_answer += 1
            denom = cq.times_in_good_answer + cq.times_in_bad_answer
            cq.quality_score = (cq.times_in_good_answer / denom) if denom else 0.5
            cq.updated_at = datetime.now(timezone.utc).isoformat()
            self._data["chunk_quality"][chunk_id] = cq.model_dump()
            self._save()

    def get_chunk_quality(self, chunk_id: str) -> Optional[ChunkQuality]:
        with self._lock:
            self._load()
            item = self._data["chunk_quality"].get(chunk_id)
        return ChunkQuality(**item) if item else None

    # ===== ingestion tasks =====
    def enqueue_task(self, task: IngestionTask) -> None:
        self._upsert("ingestion_tasks", task.model_dump())

    def claim_pending_tasks(self, limit: int = 5) -> list[IngestionTask]:
        items = [i for i in self._all("ingestion_tasks") if i.get("status") == "queued"]
        return [IngestionTask(**i) for i in items[:limit]]

    def update_task(self, task: IngestionTask) -> None:
        self._upsert("ingestion_tasks", task.model_dump())

    # ===== learning cleanup =====
    def clear_learning_state(self) -> dict:
        """Wipe all learning artifacts: feedback, rules, golden pairs, chunk quality."""
        with self._lock:
            self._load()
            counts = {
                "feedback": len(self._data.get("feedback", {})),
                "learned_rules": len(self._data.get("learned_rules", {})),
                "golden_pairs": len(self._data.get("golden_pairs", {})),
                "chunk_quality": len(self._data.get("chunk_quality", {})),
            }
            for k in ("feedback", "learned_rules", "golden_pairs", "chunk_quality"):
                self._data[k] = {}
            self._save()
        return counts
