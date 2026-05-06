"""
worker.py — Background worker for ingestion + periodic learning.

Runs two loops in a single process:

1. **Ingestion loop** — polls Cosmos for queued tasks, runs the
   `IngestionPipeline` synchronously, marks the task `done`/`failed`.
2. **Learning loop** — every `LEARN_INTERVAL_SECONDS` runs the
   self-improvement loop.

Designed to run as a separate K8s `Deployment` from the API.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from src.blob_client import BlobService
from src.cosmos_client import create_state_service
from src.doc_intelligence import DocIntelService
from src.ingestion import IngestionPipeline
from src.learning import LearningLoop
from src.openai_client import OpenAIService
from src.search_client import SearchService

import config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("worker")

POLL_INTERVAL_SECONDS = 5
LEARN_INTERVAL_SECONDS = 3600  # hourly


def main() -> None:
    blob = BlobService()
    search = SearchService()
    doc_intel = DocIntelService()
    openai_svc = OpenAIService()
    cosmos = create_state_service()

    # bootstrap (idempotent, best-effort)
    for name, fn in (
        ("blob.ensure_container", blob.ensure_container),
        ("user_input_container", lambda: blob.ensure_container_named(config.USER_UPLOAD_CONTAINER)),
        ("cosmos.ensure_containers", cosmos.ensure_containers),
        ("search.create_or_update_index", search.create_or_update_index),
    ):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            log.warning("Startup step %s failed (continuing): %s", name, exc)

    pipeline = IngestionPipeline(blob, doc_intel, openai_svc, search, cosmos)
    learner = LearningLoop(cosmos, openai_svc)

    last_learn = 0.0
    log.info("Worker started")

    while True:
        try:
            # --- Ingestion ---
            tasks = cosmos.claim_pending_tasks(limit=3)
            for task in tasks:
                task.status = "running"
                task.started_at = datetime.now(timezone.utc).isoformat()
                cosmos.update_task(task)
                try:
                    doc = cosmos.get_document(task.doc_id, user_id=task.user_id)
                    if doc is None:
                        raise RuntimeError(f"doc {task.doc_id} not found")
                    pipeline.process_pdf(doc)
                    task.status = "done"
                except Exception as e:
                    log.exception("Task failed")
                    task.status = "failed"
                    task.error = str(e)[:500]
                task.finished_at = datetime.now(timezone.utc).isoformat()
                cosmos.update_task(task)

            # --- Learning ---
            now = time.time()
            if now - last_learn >= LEARN_INTERVAL_SECONDS:
                try:
                    learner.run_once()
                except Exception:
                    log.exception("Learning loop failed")
                last_learn = now

        except Exception:
            log.exception("Worker iteration crashed")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
