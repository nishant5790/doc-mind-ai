"""Combined end-to-end check: ingestion -> RAG -> self-learning.

Runs all three stages against the live Azure backends configured in `.env`:

  1. Ingest a PDF (Blob -> Document Intelligence -> images/vision -> embeddings -> Search index)
  2. Ask a question via the RAG engine (sync answer + streamed answer)
  3. Seed feedback and run the self-learning loop (rules / golden pairs / chunk quality)

Usage (from repo root, venv active):

    python tests/test_main.py                       # all stages
    python tests/test_main.py --stage ingest
    python tests/test_main.py --stage rag
    python tests/test_main.py --stage learn
    python tests/test_main.py --question "what is the architecture?"
    python tests/test_main.py --pdf path\to\file.pdf

Drop a PDF at `notebooks/Multi_Agent_Research_System_Architecture.pdf`
or `tests/sample.pdf`, or pass `--pdf`.
"""
import _bootstrap

import argparse
import asyncio
import pathlib

from src.blob_client import BlobService
from src.search_client import SearchService
from src.doc_intelligence import DocIntelService
from src.openai_client import OpenAIService
from src.cosmos_client import create_state_service
from src.ingestion import IngestionPipeline
from src.rag import RAGEngine
from src.learning import LearningLoop
from src.models import DocumentMeta, FeedbackRecord


SESSION_ID = "test-main-session"
USER_ID = "test-main-user"


def banner(title: str) -> None:
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def run_ingest(pdf_path: pathlib.Path) -> DocumentMeta:
    banner(f"1) INGEST  -  {pdf_path.name}")

    blob = BlobService()
    blob.ensure_container()
    search = SearchService()
    search.create_or_update_index()
    cosmos = create_state_service()
    cosmos.ensure_containers()
    print("State backend:", type(cosmos).__name__)

    pipeline = IngestionPipeline(
        blob, DocIntelService(), OpenAIService(), search, cosmos
    )

    doc = DocumentMeta(
        user_id=USER_ID,
        filename=pdf_path.name,
        blob_name=f"{USER_ID}/{pdf_path.name}",
    )
    blob.upload(doc.blob_name, pdf_path.read_bytes(), content_type="application/pdf")
    cosmos.save_document(doc)
    print("Doc id:", doc.id)

    doc = pipeline.process_pdf(doc)
    print("Status :", doc.status)
    print("Pages  :", doc.total_pages)
    print("Chunks :", doc.total_chunks)
    print("Images :", doc.total_images)
    print("Tables :", doc.total_tables)
    return doc


def run_rag(question: str) -> None:
    banner(f"2) RAG  -  {question!r}")

    state = create_state_service()
    rag = RAGEngine(SearchService(), OpenAIService(), state)

    answer, sources, _turn = rag.answer(
        question, session_id=SESSION_ID, user_id=USER_ID
    )
    print("--- answer ---")
    print(answer)
    print("\n--- sources ---")
    for s in sources:
        print(f"  {s.doc_id}  page={s.page}  type={s.type}  {s.snippet[:60]}...")

    print("\n--- streaming variant ---")

    async def stream() -> None:
        async for evt in rag.stream_answer(question, SESSION_ID, USER_ID):
            if evt["type"] == "token":
                print(evt["content"], end="", flush=True)
            elif evt["type"] == "sources":
                print(f"\n[{len(evt['sources'])} sources]")
            elif evt["type"] == "done":
                print("\n--- done turn_id=", evt["turn_id"])

    asyncio.run(stream())


def run_learn() -> None:
    banner("3) SELF-LEARNING")

    cosmos = create_state_service()
    cosmos.ensure_containers()
    learner = LearningLoop(cosmos, OpenAIService())

    # Seed thumbs-down corrections
    for question, bad, fix in [
        ("How many nodes in the cluster?", "5", "Actually 3 — page 4 shows 3 nodes."),
        ("What is the SLA?", "99.9%", "It is 99.95% per the SRE doc."),
        ("When was the report published?", "2024", "It was published in Q3 2025."),
    ]:
        cosmos.save_feedback(FeedbackRecord(
            session_id=SESSION_ID, turn_id="dummy", rating="down",
            correction=fix, question=question, answer=bad, chunk_ids=["chunk-x"],
        ))

    # Seed thumbs-ups
    for question, good in [
        ("What is DocMind?", "A self-improving multimodal RAG agent."),
        ("Where is the SLA defined?", "On page 12 of the SRE document."),
    ]:
        cosmos.save_feedback(FeedbackRecord(
            session_id=SESSION_ID, turn_id="dummy", rating="up",
            question=question, answer=good, chunk_ids=["chunk-y"],
        ))
    print("feedback seeded OK")

    stats = learner.run_once()
    print("Learning stats:", stats)

    print("\nRules learned:")
    for r in cosmos.get_rules():
        print(" -", r.rule)

    print("\nGolden pairs:")
    for g in cosmos.get_golden_pairs():
        print(" Q:", g.question, "->", g.answer[:80])

    print("\nChunk quality:")
    print("  chunk-x:", cosmos.get_chunk_quality("chunk-x"))
    print("  chunk-y:", cosmos.get_chunk_quality("chunk-y"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--stage",
        choices=["all", "ingest", "rag", "learn"],
        default="all",
        help="Which stage to run (default: all).",
    )
    parser.add_argument(
        "--pdf",
        type=pathlib.Path,
        default=None,
        help="Path to a PDF for ingestion.",
    )
    parser.add_argument(
        "--question",
        default="what is the architecture of the project",
        help="Question for the RAG stage.",
    )
    args = parser.parse_args()

    if args.stage in ("all", "ingest"):
        pdf = args.pdf or _bootstrap.find_asset(
            "Multi_Agent_Research_System_Architecture.pdf",
            "sample.pdf",
        )
        run_ingest(pathlib.Path(pdf))

    if args.stage in ("all", "rag"):
        run_rag(args.question)

    if args.stage in ("all", "learn"):
        run_learn()

    banner("DONE")


if __name__ == "__main__":
    main()
