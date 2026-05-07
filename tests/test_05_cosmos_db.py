"""05 — State backend smoke test (mirrors notebooks/05_cosmos_db.ipynb)."""
import _bootstrap

from src.cosmos_client import create_state_service
from src.models import (
    ChatTurn,
    DocumentMeta,
    FeedbackRecord,
    LearnedRule,
    GoldenPair,
)


def main() -> None:
    cosmos = create_state_service()
    cosmos.ensure_containers()
    print("Backend:", type(cosmos).__name__)

    # Sessions
    t1 = ChatTurn(session_id="s-test", role="user", content="hi")
    t2 = ChatTurn(session_id="s-test", role="assistant", content="hello")
    cosmos.save_turn(t1)
    cosmos.save_turn(t2)
    print("History:")
    for h in cosmos.get_history("s-test"):
        print(" ", h.role, ":", h.content)

    # Documents
    doc = DocumentMeta(user_id="nb-user", filename="t.pdf", blob_name="nb/t.pdf")
    cosmos.save_document(doc)
    print("Documents:", cosmos.list_documents("nb-user"))

    # Feedback + rules + golden
    fb = FeedbackRecord(
        session_id="s-test",
        turn_id=t2.id,
        rating="down",
        correction="be more concise",
        question="hi",
        answer="hello",
    )
    cosmos.save_feedback(fb)
    cosmos.save_rule(LearnedRule(category="general", rule="Always be concise"))
    cosmos.save_golden(GoldenPair(topic="general", question="hi", answer="hello"))
    print("rules :", cosmos.get_rules())
    print("golden:", cosmos.get_golden_pairs())

    # Chunk quality
    cosmos.update_chunk_quality("chunk-x", retrieved=True, good=True)
    cosmos.update_chunk_quality("chunk-x", good=True)
    cosmos.update_chunk_quality("chunk-x", bad=True)
    print("chunk-x quality:", cosmos.get_chunk_quality("chunk-x"))

    # Cleanup
    cosmos.delete_document(doc.id, user_id="nb-user")
    print("cleaned OK")


if __name__ == "__main__":
    main()
