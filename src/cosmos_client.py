"""
src/cosmos_client.py — Azure Cosmos DB (NoSQL API) wrapper.

`CosmosService` manages all stateful data:
* `sessions`         — chat history (partition: /session_id)
* `documents`        — uploaded doc metadata (partition: /user_id)
* `feedback`         — 👍/👎 + corrections (partition: /session_id)
* `learned_rules`    — aggregated rules from feedback (partition: /category)
* `golden_pairs`     — confirmed Q&A few-shot examples (partition: /topic)
* `chunk_quality`    — per-chunk quality scores (partition: /chunk_id)
* `ingestion_tasks`  — work queue for the worker (partition: /status)
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from azure.cosmos import CosmosClient, PartitionKey, ContainerProxy
from azure.cosmos.exceptions import CosmosHttpResponseError

import config
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


_CONTAINERS: list[tuple[str, str]] = [
    (config.COSMOS_CONTAINER_SESSIONS, "/session_id"),
    (config.COSMOS_CONTAINER_DOCUMENTS, "/user_id"),
    (config.COSMOS_CONTAINER_FEEDBACK, "/session_id"),
    (config.COSMOS_CONTAINER_RULES, "/category"),
    (config.COSMOS_CONTAINER_GOLDEN, "/topic"),
    (config.COSMOS_CONTAINER_CHUNK_QUALITY, "/chunk_id"),
    (config.COSMOS_CONTAINER_TASKS, "/status"),
]


class CosmosService:
    """Wrapper that owns one database and all its containers."""

    def __init__(
        self,
        endpoint: str = config.COSMOS_ENDPOINT,
        key: Optional[str] = config.COSMOS_KEY,
        database: str = config.COSMOS_DATABASE,
    ) -> None:
        try:
            if key:
                self._client = CosmosClient(endpoint, credential=key)
            else:
                self._client = CosmosClient(endpoint, credential=config.CREDENTIAL)
            self._db = self._client.create_database_if_not_exists(id=database)
        except CosmosHttpResponseError as exc:
            if getattr(exc, "status_code", None) == 401:
                raise RuntimeError(
                    "Cosmos authentication failed. Verify COSMOS_ENDPOINT and COSMOS_KEY in .env "
                    "match the same Cosmos account, or configure managed identity credentials."
                ) from exc
            raise
        self._cache: dict[str, ContainerProxy] = {}

    # ------------------------------------------------------------------
    def ensure_containers(self) -> None:
        """Create all required containers if they don't exist."""
        for name, pk in _CONTAINERS:
            self._db.create_container_if_not_exists(id=name, partition_key=PartitionKey(path=pk))
            log.info("Cosmos container ready: %s (pk=%s)", name, pk)

    # ------------------------------------------------------------------
    def _container(self, name: str) -> ContainerProxy:
        if name not in self._cache:
            self._cache[name] = self._db.get_container_client(name)
        return self._cache[name]

    # ===== sessions =====
    def save_turn(self, turn: ChatTurn) -> None:
        self._container(config.COSMOS_CONTAINER_SESSIONS).upsert_item(turn.model_dump())

    def get_history(self, session_id: str, limit: int = 20) -> list[ChatTurn]:
        q = (
            "SELECT * FROM c WHERE c.session_id=@s ORDER BY c.created_at ASC OFFSET 0 LIMIT @l"
        )
        items = list(
            self._container(config.COSMOS_CONTAINER_SESSIONS).query_items(
                query=q,
                parameters=[{"name": "@s", "value": session_id}, {"name": "@l", "value": limit}],
                partition_key=session_id,
            )
        )
        return [ChatTurn(**i) for i in items]

    # ===== documents =====
    def save_document(self, doc: DocumentMeta) -> None:
        self._container(config.COSMOS_CONTAINER_DOCUMENTS).upsert_item(doc.model_dump())

    def get_document(self, doc_id: str, user_id: str = "anonymous") -> Optional[DocumentMeta]:
        try:
            item = self._container(config.COSMOS_CONTAINER_DOCUMENTS).read_item(
                item=doc_id, partition_key=user_id
            )
            return DocumentMeta(**item)
        except Exception:
            return None

    def list_documents(self, user_id: str = "anonymous") -> list[DocumentMeta]:
        items = list(
            self._container(config.COSMOS_CONTAINER_DOCUMENTS).query_items(
                query="SELECT * FROM c WHERE c.user_id=@u",
                parameters=[{"name": "@u", "value": user_id}],
                partition_key=user_id,
            )
        )
        return [DocumentMeta(**i) for i in items]

    def delete_document(self, doc_id: str, user_id: str = "anonymous") -> None:
        self._container(config.COSMOS_CONTAINER_DOCUMENTS).delete_item(item=doc_id, partition_key=user_id)

    # ===== feedback =====
    def save_feedback(self, fb: FeedbackRecord) -> None:
        self._container(config.COSMOS_CONTAINER_FEEDBACK).upsert_item(fb.model_dump())

    def list_feedback(self, limit: int = 200) -> list[FeedbackRecord]:
        items = list(
            self._container(config.COSMOS_CONTAINER_FEEDBACK).query_items(
                query="SELECT TOP @l * FROM c ORDER BY c.created_at DESC",
                parameters=[{"name": "@l", "value": limit}],
                enable_cross_partition_query=True,
            )
        )
        return [FeedbackRecord(**i) for i in items]

    # ===== learned rules =====
    def save_rule(self, rule: LearnedRule) -> None:
        self._container(config.COSMOS_CONTAINER_RULES).upsert_item(rule.model_dump())

    def get_rules(self, category: str = "general", top: int = 5) -> list[LearnedRule]:
        items = list(
            self._container(config.COSMOS_CONTAINER_RULES).query_items(
                query="SELECT TOP @t * FROM c WHERE c.category=@c ORDER BY c.evidence_count DESC",
                parameters=[{"name": "@c", "value": category}, {"name": "@t", "value": top}],
                partition_key=category,
            )
        )
        return [LearnedRule(**i) for i in items]

    def list_rules(self, limit: int = 100) -> list[LearnedRule]:
        items = list(
            self._container(config.COSMOS_CONTAINER_RULES).query_items(
                query="SELECT TOP @l * FROM c ORDER BY c.updated_at DESC",
                parameters=[{"name": "@l", "value": limit}],
                enable_cross_partition_query=True,
            )
        )
        return [LearnedRule(**i) for i in items]

    # ===== golden pairs =====
    def save_golden(self, gp: GoldenPair) -> None:
        self._container(config.COSMOS_CONTAINER_GOLDEN).upsert_item(gp.model_dump())

    def get_golden_pairs(self, topic: str = "general", top: int = 3) -> list[GoldenPair]:
        items = list(
            self._container(config.COSMOS_CONTAINER_GOLDEN).query_items(
                query="SELECT TOP @t * FROM c WHERE c.topic=@p ORDER BY c.confirmed_at DESC",
                parameters=[{"name": "@p", "value": topic}, {"name": "@t", "value": top}],
                partition_key=topic,
            )
        )
        return [GoldenPair(**i) for i in items]

    def list_golden_pairs(self, limit: int = 100) -> list[GoldenPair]:
        items = list(
            self._container(config.COSMOS_CONTAINER_GOLDEN).query_items(
                query="SELECT TOP @l * FROM c ORDER BY c.confirmed_at DESC",
                parameters=[{"name": "@l", "value": limit}],
                enable_cross_partition_query=True,
            )
        )
        return [GoldenPair(**i) for i in items]

    # ===== chunk quality =====
    def update_chunk_quality(self, chunk_id: str, *, retrieved: bool = False, good: bool = False, bad: bool = False) -> None:
        c = self._container(config.COSMOS_CONTAINER_CHUNK_QUALITY)
        try:
            item = c.read_item(item=chunk_id, partition_key=chunk_id)
            cq = ChunkQuality(**item)
        except Exception:
            cq = ChunkQuality(id=chunk_id, chunk_id=chunk_id)
        if retrieved:
            cq.times_retrieved += 1
        if good:
            cq.times_in_good_answer += 1
        if bad:
            cq.times_in_bad_answer += 1
        denom = cq.times_in_good_answer + cq.times_in_bad_answer
        cq.quality_score = (cq.times_in_good_answer / denom) if denom else 0.5
        c.upsert_item(cq.model_dump())

    def get_chunk_quality(self, chunk_id: str) -> Optional[ChunkQuality]:
        try:
            item = self._container(config.COSMOS_CONTAINER_CHUNK_QUALITY).read_item(
                item=chunk_id, partition_key=chunk_id
            )
            return ChunkQuality(**item)
        except Exception:
            return None

    # ===== ingestion tasks =====
    def enqueue_task(self, task: IngestionTask) -> None:
        self._container(config.COSMOS_CONTAINER_TASKS).upsert_item(task.model_dump())

    def claim_pending_tasks(self, limit: int = 5) -> list[IngestionTask]:
        items = list(
            self._container(config.COSMOS_CONTAINER_TASKS).query_items(
                query="SELECT TOP @l * FROM c WHERE c.status='queued'",
                parameters=[{"name": "@l", "value": limit}],
                partition_key="queued",
            )
        )
        return [IngestionTask(**i) for i in items]

    def update_task(self, task: IngestionTask) -> None:
        self._container(config.COSMOS_CONTAINER_TASKS).upsert_item(task.model_dump())

    # ===== learning cleanup =====
    def clear_learning_state(self) -> dict:
        """Delete every item from feedback, learned_rules, golden_pairs and chunk_quality."""
        targets = [
            (config.COSMOS_CONTAINER_FEEDBACK, "session_id"),
            (config.COSMOS_CONTAINER_RULES, "category"),
            (config.COSMOS_CONTAINER_GOLDEN, "topic"),
            (config.COSMOS_CONTAINER_CHUNK_QUALITY, "chunk_id"),
        ]
        counts: dict[str, int] = {}
        for name, pk_field in targets:
            c = self._container(name)
            items = list(
                c.query_items(
                    query=f"SELECT c.id, c.{pk_field} AS pk FROM c",
                    enable_cross_partition_query=True,
                )
            )
            n = 0
            for it in items:
                try:
                    c.delete_item(item=it["id"], partition_key=it.get("pk"))
                    n += 1
                except Exception as e:  # noqa: BLE001
                    log.warning("delete from %s failed for %s: %s", name, it.get("id"), e)
            counts[name] = n
        return counts

# ---------------------------------------------------------------------------
# Factory - pick Cosmos when configured, else use the local file backend.
# ---------------------------------------------------------------------------
def create_state_service():
    """Return CosmosService if COSMOS_ENDPOINT+COSMOS_KEY are set, else LocalStateService.

    Falls back to LocalStateService if Cosmos initialization fails so the app
    can still boot in environments without Cosmos connectivity.
    """
    from src.local_state import LocalStateService

    endpoint = (config.COSMOS_ENDPOINT or "").strip()
    key = (config.COSMOS_KEY or "").strip() if config.COSMOS_KEY else ""
    if not endpoint or not key:
        log.warning("COSMOS_ENDPOINT/COSMOS_KEY not set - using LocalStateService")
        return LocalStateService()
    try:
        return CosmosService()
    except Exception as exc:  # noqa: BLE001
        log.error("Cosmos initialization failed (%s) - falling back to LocalStateService", exc)
        return LocalStateService()
