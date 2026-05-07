"""04 — AI Search smoke test (mirrors notebooks/04_ai_search.ipynb)."""
import _bootstrap

from src.search_client import SearchService
from src.openai_client import OpenAIService
from src.models import ChunkRecord


def main() -> None:
    search = SearchService()
    search.create_or_update_index()
    ai = OpenAIService()

    texts = [
        ("chunk-test-1", "The Eiffel Tower is in Paris and is 330 meters tall."),
        ("chunk-test-2", "The Statue of Liberty stands in New York Harbor."),
    ]
    vectors = ai.embed([t for _, t in texts])
    chunks = [
        ChunkRecord(
            id=cid, doc_id="test-doc", page=1, type="text", content=t, embedding=v
        )
        for (cid, t), v in zip(texts, vectors)
    ]
    search.index_chunks(chunks)
    print(f"Indexed {len(chunks)} test chunks.")

    q = "How tall is the Eiffel Tower?"
    qv = ai.embed(q)[0]
    print(f"Query: {q}")
    for s in search.search(q, qv, top_k=2):
        print(f"  {s.chunk_id}  page={s.page}  -> {s.snippet[:120]}")

    search.delete_document("test-doc")
    print("Removed test chunks OK")


if __name__ == "__main__":
    main()
