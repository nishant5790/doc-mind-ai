"""07 — RAG query (retrieval + LLM). Mirrors notebooks/07_rag_query.ipynb.

Run test_06_ingestion_pipeline.py first so the index has data.
"""
import _bootstrap
import asyncio

from src.search_client import SearchService
from src.openai_client import OpenAIService
from src.cosmos_client import create_state_service
from src.rag import RAGEngine


QUESTION = "what is the architecture of the project"


def main() -> None:
    state = create_state_service()
    print("State backend:", type(state).__name__)
    rag = RAGEngine(SearchService(), OpenAIService(), state)

    answer, sources, _turn = rag.answer(
        QUESTION, session_id="nb-rag-session", user_id="nb-user"
    )
    print("--- answer ---")
    print(answer)
    print("\n--- sources ---")
    for s in sources:
        print(f"  {s.doc_id}  page={s.page}  type={s.type}  {s.snippet[:60]}...")

    print("\n--- streaming ---")

    async def run_stream() -> None:
        async for evt in rag.stream_answer(
            "What images appear in this doc?", "nb-rag-session", "nb-user"
        ):
            if evt["type"] == "token":
                print(evt["content"], end="", flush=True)
            elif evt["type"] == "sources":
                print(f"[{len(evt['sources'])} sources]\n")
            elif evt["type"] == "done":
                print("\n--- done turn_id=", evt["turn_id"])

    asyncio.run(run_stream())


if __name__ == "__main__":
    main()
