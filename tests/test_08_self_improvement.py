"""08 — Self-improvement loop (mirrors notebooks/08_self_improvement.ipynb)."""
import _bootstrap

from src.cosmos_client import create_state_service
from src.openai_client import OpenAIService
from src.learning import LearningLoop
from src.models import FeedbackRecord


def main() -> None:
    cosmos = create_state_service()
    cosmos.ensure_containers()
    print("State backend:", type(cosmos).__name__)

    learner = LearningLoop(cosmos, OpenAIService())

    # Seed three thumbs-down corrections
    for question, bad, fix in [
        ("How many nodes in the cluster?", "5", "Actually 3 — page 4 shows 3 nodes."),
        ("What is the SLA?", "99.9%", "It is 99.95% per the SRE doc."),
        ("When was the report published?", "2024", "It was published in Q3 2025."),
    ]:
        cosmos.save_feedback(FeedbackRecord(
            session_id="nb-learn", turn_id="dummy", rating="down",
            correction=fix, question=question, answer=bad, chunk_ids=["chunk-x"],
        ))

    # Seed two thumbs-ups
    for question, good in [
        ("What is DocMind?", "A self-improving multimodal RAG agent."),
        ("Where is the SLA defined?", "On page 12 of the SRE document."),
    ]:
        cosmos.save_feedback(FeedbackRecord(
            session_id="nb-learn", turn_id="dummy", rating="up",
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


if __name__ == "__main__":
    main()
