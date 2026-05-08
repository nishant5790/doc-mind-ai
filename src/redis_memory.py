"""
src/redis_memory.py — Redis-backed chat memory.

Stores chat history per session so it survives:
  * UI tab switches / page reloads
  * API restarts (chat history is in Redis, not process memory)

Storage layout (all keys are JSON-encoded values):
  {prefix}:turn:{session_id}        -> Redis LIST of ChatTurn JSON strings
                                        (RPUSH appends, LRANGE reads in order)
  {prefix}:session:{user_id}        -> Redis HASH mapping session_id -> JSON
                                        metadata {title, updated_at, user_id}

Defaults to the in-process `_InMemoryFallback` if the Redis URL is not set
or the connection cannot be established. The public API matches what the
rest of the app needs (`save_turn`, `get_history`, `list_sessions`,
`delete_session`) so swapping in Azure Cache for Redis later only requires
changing `REDIS_URL`.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

from src.models import ChatTurn

import config

log = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class _InMemoryFallback:
    """Used only when Redis is not reachable. Survives within one process."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._turns: dict[str, list[str]] = {}
        self._sessions: dict[str, dict[str, str]] = {}  # user_id -> {sid: meta_json}

    def rpush(self, key: str, value: str) -> None:
        with self._lock:
            self._turns.setdefault(key, []).append(value)

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        with self._lock:
            buf = self._turns.get(key, [])
            if end == -1:
                return list(buf[start:])
            return list(buf[start : end + 1])

    def delete(self, key: str) -> None:
        with self._lock:
            self._turns.pop(key, None)

    def hset(self, key: str, field: str, value: str) -> None:
        with self._lock:
            self._sessions.setdefault(key, {})[field] = value

    def hgetall(self, key: str) -> dict[str, str]:
        with self._lock:
            return dict(self._sessions.get(key, {}))

    def hdel(self, key: str, field: str) -> None:
        with self._lock:
            self._sessions.get(key, {}).pop(field, None)


class RedisChatMemory:
    """Persistent chat-history store backed by Redis."""

    def __init__(self, url: Optional[str] = None, prefix: str = "docmind", history_limit: int = config.REDIS_HISTORY_LIMIT) -> None:
        self._prefix = prefix
        self._history_limit = history_limit
        self._client = None
        if url:
            try:
                import redis  # type: ignore
                from redis.retry import Retry  # type: ignore
                from redis.backoff import ExponentialBackoff  # type: ignore

                # Production-grade settings:
                #   - socket_timeout / socket_connect_timeout: avoid hangs.
                #   - health_check_interval: Azure Cache for Redis closes idle
                #     connections after ~10 min; periodic PINGs keep the pool warm.
                #   - retry on TimeoutError/ConnectionError with exponential backoff.
                #   - rediss:// URLs automatically enable TLS.
                self._client = redis.Redis.from_url(
                    url,
                    decode_responses=True,
                    socket_timeout=5,
                    socket_connect_timeout=5,
                    socket_keepalive=True,
                    health_check_interval=30,
                    retry=Retry(ExponentialBackoff(cap=10, base=1), retries=3),
                    retry_on_timeout=True,
                    retry_on_error=[redis.exceptions.ConnectionError, redis.exceptions.TimeoutError],
                )
                self._client.ping()
                # Don't log the URL — it may contain an access key.
                scheme = url.split("://", 1)[0]
                log.info("RedisChatMemory connected (%s, TLS=%s)", scheme, scheme == "rediss")
            except Exception as exc:  # noqa: BLE001
                log.warning("Redis unavailable (%s) — using in-process fallback", exc)
                self._client = None
        else:
            log.warning("REDIS_URL not set — using in-process chat memory fallback")
        if self._client is None:
            self._client = _InMemoryFallback()

    # ------------------------------------------------------------------
    def _turn_key(self, session_id: str) -> str:
        return f"{self._prefix}:turn:{session_id}"

    def _sessions_key(self, user_id: str) -> str:
        return f"{self._prefix}:session:{user_id}"

    # ------------------------------------------------------------------
    def save_turn(self, turn: ChatTurn) -> None:
        payload = json.dumps(turn.model_dump(), default=str)
        self._client.rpush(self._turn_key(turn.session_id), payload)
        # Update session index (title = first user message, truncated).
        idx_key = self._sessions_key(turn.user_id)
        existing = self._client.hgetall(idx_key) or {}
        meta_raw = existing.get(turn.session_id)
        if meta_raw:
            try:
                meta = json.loads(meta_raw)
            except Exception:  # noqa: BLE001
                meta = {}
        else:
            meta = {}
        if turn.role == "user" and not meta.get("title"):
            meta["title"] = (turn.content or "Untitled").strip()[:80]
        meta["updated_at"] = _utcnow()
        meta["session_id"] = turn.session_id
        meta["user_id"] = turn.user_id
        self._client.hset(idx_key, turn.session_id, json.dumps(meta))

    def get_history(self, session_id: str, limit: int = 50) -> list[ChatTurn]:
        raw = self._client.lrange(self._turn_key(session_id), 0, -1) or []
        turns: list[ChatTurn] = []
        for item in raw[-limit:]:
            try:
                turns.append(ChatTurn(**json.loads(item)))
            except Exception:  # noqa: BLE001
                log.exception("Bad turn payload in Redis for %s", session_id)
        return turns

    def list_sessions(self, user_id: str) -> list[dict]:
        raw = self._client.hgetall(self._sessions_key(user_id)) or {}
        out: list[dict] = []
        for _, meta_raw in raw.items():
            try:
                out.append(json.loads(meta_raw))
            except Exception:  # noqa: BLE001
                continue
        out.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
        return out

    def delete_session(self, session_id: str, user_id: str) -> None:
        self._client.delete(self._turn_key(session_id))
        self._client.hdel(self._sessions_key(user_id), session_id)

    def flush_all(self) -> None:
        """Wipe every chat-history and session-index entry under this prefix.

        On a real Redis client, deletes all keys matching ``{prefix}:*``.
        On the in-process fallback, clears the in-memory dicts.
        """
        client = self._client
        # In-process fallback exposes raw dicts.
        if isinstance(client, _InMemoryFallback):
            with client._lock:  # noqa: SLF001
                client._turns.clear()  # noqa: SLF001
                client._sessions.clear()  # noqa: SLF001
            return
        try:
            pattern = f"{self._prefix}:*"
            # SCAN+DELETE keeps things safe even if the keyspace is large.
            for key in client.scan_iter(match=pattern, count=500):
                try:
                    client.delete(key)
                except Exception as exc:  # noqa: BLE001
                    log.warning("flush_all: failed to delete %s: %s", key, exc)
        except Exception:
            log.exception("flush_all failed; attempting FLUSHDB as a last resort")
            try:
                client.flushdb()
            except Exception:
                log.exception("FLUSHDB also failed")


def create_chat_memory() -> RedisChatMemory:
    """Factory: read REDIS_URL from config and return a memory instance."""
    import config

    return RedisChatMemory(
        url=getattr(config, "REDIS_URL", None) or None,
        prefix=getattr(config, "REDIS_PREFIX", "docmind"),
    )
