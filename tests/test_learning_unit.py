"""
tests/test_learning_unit.py — Rigorous unit & integration tests for the
self-improvement loop (`src/learning.py`) and the RAG quality-filter
(`src/rag.py::RAGEngine.retrieve`).

What we cover
-------------
1. **Style detector** — `_is_style_only_correction` and `_removal_target_types`
   classify real corrections correctly (bullets vs. remove-image vs. factual).
2. **Layer 1 — Chunk quality**
   * 👍 increments good counter and lifts `quality_score`.
   * 👎 with a *factual* correction increments bad counter on every cited chunk.
   * 👎 with a *style-only* correction does NOT touch chunk quality.
   * 👎 with a *targeted removal* correction (e.g. "remove the image on
     page 11") penalises ONLY the matching modality, not the text chunk on
     the same page.
3. **Layer 2 — Rule distillation** — feeds corrections through a stubbed
   OpenAIService and verifies the resulting `LearnedRule` rows are stored,
   normalised, and de-duplicated by content.
4. **Layer 3 — Golden Q&A pairs** — only 👍 turns with question+answer
   become `GoldenPair` rows.
5. **End-to-end retrieval impact** — after a targeted-removal feedback
   round, `RAGEngine.retrieve` actually drops the offending image chunk
   while keeping the text chunk, but never returns an empty source list.

Run with:

    pytest -q tests/test_learning_unit.py
"""

from __future__ import annotations

import json
import sys
import pathlib
from typing import Iterable

import pytest

# Ensure repo root is importable.
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.learning import LearningLoop  # noqa: E402
from src.local_state import LocalStateService  # noqa: E402
from src.models import (  # noqa: E402
    ChunkQuality,
    FeedbackChunkMeta,
    FeedbackRecord,
    Source,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class FakeOpenAI:
    """Minimal OpenAIService double.

    `chat()` returns whatever was queued via `enqueue()`. The tests use it
    to drive the rule-distillation prompt deterministically without
    calling out to Azure. `embed()` returns a fixed vector so the
    retrieve-pipeline tests are reproducible.
    """

    def __init__(self) -> None:
        self._chat_responses: list[str] = []
        self.chat_calls: list[list[dict]] = []

    def enqueue(self, *responses: str) -> None:
        self._chat_responses.extend(responses)

    # signature mirrors OpenAIService.chat
    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str:
        self.chat_calls.append(messages)
        if not self._chat_responses:
            raise AssertionError(
                "FakeOpenAI.chat() called but no response queued"
            )
        return self._chat_responses.pop(0)

    # mirror OpenAIService.embed
    def embed(self, text):  # noqa: ANN001
        if isinstance(text, str):
            return [[0.1] * 8]
        return [[0.1] * 8 for _ in text]


class FakeSearch:
    """Tiny SearchService double for retrieve() tests."""

    def __init__(self, sources: list[Source]) -> None:
        self.sources = sources

    def search(
        self,
        question: str,
        embedding,  # noqa: ANN001
        top_k: int = 5,
        doc_ids=None,  # noqa: ANN001
        type_filter: str | None = None,
    ) -> list[Source]:
        if type_filter:
            return [s for s in self.sources if s.type == type_filter][:top_k]
        return list(self.sources[:top_k])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def state(tmp_path: pathlib.Path) -> LocalStateService:
    """Real file-backed state service in an isolated temp dir."""
    return LocalStateService(path=str(tmp_path / "state.json"))


@pytest.fixture()
def fake_openai() -> FakeOpenAI:
    return FakeOpenAI()


@pytest.fixture()
def loop(state: LocalStateService, fake_openai: FakeOpenAI) -> LearningLoop:
    return LearningLoop(state, fake_openai)  # type: ignore[arg-type]


def _seed_feedback(
    state: LocalStateService, *records: FeedbackRecord
) -> None:
    for r in records:
        state.save_feedback(r)


# ===========================================================================
# 1) Correction classifiers
# ===========================================================================
class TestCorrectionClassifiers:
    @pytest.mark.parametrize(
        "correction, expected",
        [
            ("answer in detail in bulleted points", True),
            ("please use bullet points and be concise", True),
            ("rewrite this as a numbered list", True),
            ("Actually 3 — page 4 shows 3 nodes.", False),
            ("It is 99.95% per the SRE doc.", False),
            ("", False),
            (None, False),
            # Removal intent dominates over the word "image" being a
            # would-be style hint — should NOT be classified as style-only.
            ("remove the image on page 11 that is unnecessary", False),
        ],
    )
    def test_is_style_only(self, correction, expected):  # noqa: ANN001
        assert LearningLoop._is_style_only_correction(correction or "") is expected

    @pytest.mark.parametrize(
        "correction, expected",
        [
            ("remove the image on page 11 and page 6 that is unnecessary",
             {"image"}),
            ("don't show the diagram, it's irrelevant", {"image"}),
            ("drop the table on page 4", {"table"}),
            ("remove the figure and the table", {"image", "table"}),
            ("answer in bullet points", set()),
            ("Actually it's 3 nodes", set()),
            ("", set()),
        ],
    )
    def test_removal_targets(self, correction, expected):  # noqa: ANN001
        assert LearningLoop._removal_target_types(correction) == expected


# ===========================================================================
# 2) Chunk-quality layer
# ===========================================================================
class TestChunkQuality:
    def test_thumbs_up_marks_chunks_good(
        self, state: LocalStateService, loop: LearningLoop
    ):
        _seed_feedback(
            state,
            FeedbackRecord(
                session_id="s1", turn_id="t1", rating="up",
                question="q", answer="a",
                chunk_ids=["c-good"],
            ),
        )
        # No corrections -> rule distillation is skipped, no LLM call.
        stats = loop.run_once()
        assert stats["chunk_updates"] == 1
        cq = state.get_chunk_quality("c-good")
        assert cq is not None
        assert cq.times_in_good_answer == 1
        assert cq.times_in_bad_answer == 0
        assert cq.quality_score == pytest.approx(1.0)

    def test_factual_thumbs_down_penalises_all_chunks(
        self,
        state: LocalStateService,
        loop: LearningLoop,
        fake_openai: FakeOpenAI,
    ):
        # Stub the rule-distillation reply.
        fake_openai.enqueue(json.dumps({"rules": ["Cite the page."]}))
        _seed_feedback(
            state,
            FeedbackRecord(
                session_id="s1", turn_id="t1", rating="down",
                correction="Actually 3 nodes — page 4.",
                question="How many nodes?", answer="5",
                chunk_ids=["c1", "c2"],
                chunk_meta=[
                    FeedbackChunkMeta(chunk_id="c1", type="text", page=4),
                    FeedbackChunkMeta(chunk_id="c2", type="image", page=4),
                ],
            ),
        )
        stats = loop.run_once()
        assert stats["chunk_updates"] == 2
        assert state.get_chunk_quality("c1").times_in_bad_answer == 1
        assert state.get_chunk_quality("c2").times_in_bad_answer == 1

    def test_style_only_thumbs_down_skips_chunk_penalty(
        self,
        state: LocalStateService,
        loop: LearningLoop,
        fake_openai: FakeOpenAI,
    ):
        fake_openai.enqueue(json.dumps({"rules": ["Use bullet points."]}))
        _seed_feedback(
            state,
            FeedbackRecord(
                session_id="s1", turn_id="t1", rating="down",
                correction="answer in detail in bulleted points",
                question="What is the architecture?",
                answer="...",
                chunk_ids=["c-text", "c-img"],
                chunk_meta=[
                    FeedbackChunkMeta(chunk_id="c-text", type="text", page=13),
                    FeedbackChunkMeta(chunk_id="c-img", type="image", page=15),
                ],
            ),
        )
        stats = loop.run_once()
        assert stats["chunk_updates"] == 0
        # No quality rows should have been written for either chunk.
        assert state.get_chunk_quality("c-text") is None
        assert state.get_chunk_quality("c-img") is None
        # But the rule layer should still have learned something.
        assert stats["rules_added"] >= 1

    def test_targeted_removal_only_penalises_matching_modality(
        self,
        state: LocalStateService,
        loop: LearningLoop,
        fake_openai: FakeOpenAI,
    ):
        fake_openai.enqueue(
            json.dumps({"rules": ["Avoid referencing unnecessary images."]})
        )
        _seed_feedback(
            state,
            FeedbackRecord(
                session_id="s1", turn_id="t1", rating="down",
                correction="remove the image on page 11 and page 6 that is unneccessary",
                question="What is the architecture?",
                answer="...",
                chunk_ids=["text-13", "img-6", "img-11", "img-15"],
                chunk_meta=[
                    FeedbackChunkMeta(chunk_id="text-13", type="text", page=13),
                    FeedbackChunkMeta(chunk_id="img-6", type="image", page=6),
                    FeedbackChunkMeta(chunk_id="img-11", type="image", page=11),
                    FeedbackChunkMeta(chunk_id="img-15", type="image", page=15),
                ],
            ),
        )
        loop.run_once()
        # Text chunk must NOT be penalised.
        assert state.get_chunk_quality("text-13") is None
        # Every image chunk must be penalised hard enough to cross the
        # quality-filter threshold (default 0.3) on the next retrieval.
        for cid in ("img-6", "img-11", "img-15"):
            cq = state.get_chunk_quality(cid)
            assert cq is not None, cid
            assert cq.times_in_bad_answer >= 2, cid
            assert cq.quality_score < 0.3, cid

    def test_targeted_removal_falls_back_when_no_chunk_meta(
        self,
        state: LocalStateService,
        loop: LearningLoop,
        fake_openai: FakeOpenAI,
    ):
        """Older feedback rows have no chunk_meta — we must still penalise
        all cited chunks rather than silently skipping them."""
        fake_openai.enqueue(json.dumps({"rules": ["..."]}))
        _seed_feedback(
            state,
            FeedbackRecord(
                session_id="s1", turn_id="t1", rating="down",
                correction="remove the diagram, it's irrelevant",
                question="?", answer="!",
                chunk_ids=["legacy-1", "legacy-2"],
                # chunk_meta intentionally empty
            ),
        )
        loop.run_once()
        for cid in ("legacy-1", "legacy-2"):
            cq = state.get_chunk_quality(cid)
            assert cq is not None
            assert cq.times_in_bad_answer >= 1


# ===========================================================================
# 3) Rule distillation
# ===========================================================================
class TestRuleDistillation:
    def test_rules_are_persisted_and_normalised(
        self,
        state: LocalStateService,
        loop: LearningLoop,
        fake_openai: FakeOpenAI,
    ):
        fake_openai.enqueue(
            json.dumps(
                {
                    "rules": [
                        "Use bullet points for clarity.",
                        "Use bullet points for clarity!",  # near-duplicate
                        "  ",  # ignored — empty
                        "Cite source pages.",
                    ]
                }
            )
        )
        _seed_feedback(
            state,
            FeedbackRecord(
                session_id="s1", turn_id="t1", rating="down",
                correction="please use bullets and cite pages",
                question="q", answer="a", chunk_ids=[],
            ),
        )
        stats = loop.run_once()
        # Two unique normalised rules survive de-dup.
        assert stats["rules_added"] == 2
        rules = sorted(r.rule for r in state.list_rules())
        assert rules == ["Cite source pages.", "Use bullet points for clarity."]

    def test_no_corrections_means_no_rules(
        self,
        state: LocalStateService,
        loop: LearningLoop,
        fake_openai: FakeOpenAI,
    ):
        # 👎 with NO correction text — distillation must not be invoked.
        _seed_feedback(
            state,
            FeedbackRecord(
                session_id="s1", turn_id="t1", rating="down",
                correction=None,
                question="q", answer="a", chunk_ids=["c1"],
            ),
        )
        stats = loop.run_once()
        assert stats["rules_added"] == 0
        assert fake_openai.chat_calls == []  # LLM not called

    def test_malformed_llm_response_is_tolerated(
        self,
        state: LocalStateService,
        loop: LearningLoop,
        fake_openai: FakeOpenAI,
    ):
        fake_openai.enqueue("not-valid-json")
        _seed_feedback(
            state,
            FeedbackRecord(
                session_id="s1", turn_id="t1", rating="down",
                correction="be clearer",
                question="q", answer="a", chunk_ids=[],
            ),
        )
        stats = loop.run_once()
        assert stats["rules_added"] == 0
        # Loop must NOT crash on bad JSON.
        assert state.list_rules() == []

    def test_strip_fences_handles_code_blocks(self):
        wrapped = "```json\n{\"rules\":[\"a\"]}\n```"
        assert json.loads(LearningLoop._strip_fences(wrapped)) == {"rules": ["a"]}


# ===========================================================================
# 4) Golden Q&A promotion
# ===========================================================================
class TestGoldenPromotion:
    def test_thumbs_up_with_qa_promoted(
        self, state: LocalStateService, loop: LearningLoop
    ):
        _seed_feedback(
            state,
            FeedbackRecord(
                session_id="s1", turn_id="t1", rating="up",
                question="What is DocMind?",
                answer="A self-improving multimodal RAG agent.",
                chunk_ids=["c-y"],
            ),
        )
        stats = loop.run_once()
        assert stats["golden_added"] == 1
        gp = state.list_golden_pairs()
        assert len(gp) == 1
        assert gp[0].question == "What is DocMind?"

    def test_thumbs_down_never_promoted(
        self,
        state: LocalStateService,
        loop: LearningLoop,
        fake_openai: FakeOpenAI,
    ):
        fake_openai.enqueue(json.dumps({"rules": ["..."]}))
        _seed_feedback(
            state,
            FeedbackRecord(
                session_id="s1", turn_id="t1", rating="down",
                correction="wrong",
                question="q", answer="bad", chunk_ids=[],
            ),
        )
        stats = loop.run_once()
        assert stats["golden_added"] == 0
        assert state.list_golden_pairs() == []

    def test_thumbs_up_without_qa_skipped(
        self, state: LocalStateService, loop: LearningLoop
    ):
        _seed_feedback(
            state,
            FeedbackRecord(
                session_id="s1", turn_id="t1", rating="up",
                question="", answer="", chunk_ids=["c"],
            ),
        )
        stats = loop.run_once()
        assert stats["golden_added"] == 0


# ===========================================================================
# 5) End-to-end: retrieval reflects the learning
# ===========================================================================
class TestRetrievalIntegration:
    """Mirrors the user-reported scenario:

    * A question retrieves one text chunk + several image chunks.
    * User submits 👎 with correction "remove the image on page 11/6".
    * Learning loop runs.
    * Asking the same question again should return the text chunk and
      drop the offending images, *without ever returning an empty list*.
    """

    def _build_engine(self, state: LocalStateService, sources: list[Source]):
        # Lazy import so the test module doesn't pull RAGEngine deps unless
        # this class actually runs.
        from src.rag import RAGEngine

        fake_openai = FakeOpenAI()
        search = FakeSearch(sources)

        # _visual_intent_score calls openai.chat once if no strong keyword.
        # Pre-queue a benign "0.0" so we never block on a real call.
        fake_openai.enqueue("0.0", "0.0", "0.0")
        engine = RAGEngine(search, fake_openai, state)  # type: ignore[arg-type]
        return engine, fake_openai

    def test_targeted_removal_filters_only_images(
        self, state: LocalStateService
    ):
        from src.learning import LearningLoop

        text13 = Source(chunk_id="text-13", doc_id="d", page=13,
                        type="text", snippet="The architecture is a DAG ...")
        img6 = Source(chunk_id="img-6", doc_id="d", page=6,
                      type="image", snippet="diagram", image_url="x")
        img11 = Source(chunk_id="img-11", doc_id="d", page=11,
                       type="image", snippet="diagram", image_url="x")
        img15 = Source(chunk_id="img-15", doc_id="d", page=15,
                       type="image", snippet="diagram", image_url="x")
        sources = [text13, img6, img11, img15]

        # 1) First pass — nothing learned yet, all sources should pass through.
        engine, _ = self._build_engine(state, sources)
        first = engine.retrieve("what is the architecture of project")
        first_ids = {s.chunk_id for s in first}
        assert first_ids == {"text-13", "img-6", "img-11", "img-15"}

        # 2) User submits a targeted-removal 👎.
        state.save_feedback(
            FeedbackRecord(
                session_id="s", turn_id="t", rating="down",
                correction="remove the image on page 11 and page 6 that is unneccessary",
                question="what is the architecture of project",
                answer="...",
                chunk_ids=[s.chunk_id for s in sources],
                chunk_meta=[
                    FeedbackChunkMeta(chunk_id=s.chunk_id, type=s.type, page=s.page)
                    for s in sources
                ],
            )
        )
        learner_openai = FakeOpenAI()
        learner_openai.enqueue(json.dumps({"rules": ["Avoid unnecessary images."]}))
        LearningLoop(state, learner_openai).run_once()  # type: ignore[arg-type]

        # 3) Second pass — image chunks should be dropped, text kept.
        engine2, _ = self._build_engine(state, sources)
        second = engine2.retrieve("what is the architecture of project")
        second_ids = {s.chunk_id for s in second}
        assert "text-13" in second_ids
        assert {"img-6", "img-11", "img-15"}.isdisjoint(second_ids), (
            f"Expected all image chunks to be filtered out, got {second_ids}"
        )

    def test_never_returns_empty_when_all_chunks_flagged(
        self, state: LocalStateService
    ):
        """Safety net: if EVERY retrieved chunk is below threshold, we
        keep them all rather than returning an empty list.
        """
        bad = Source(chunk_id="b1", doc_id="d", page=1, type="text", snippet="x")
        # Pre-poison the chunk so it would normally be filtered.
        for _ in range(3):
            state.update_chunk_quality("b1", bad=True)
        cq = state.get_chunk_quality("b1")
        assert cq.quality_score < 0.3

        from src.rag import RAGEngine

        fake_openai = FakeOpenAI()
        fake_openai.enqueue("0.0")
        engine = RAGEngine(FakeSearch([bad]), fake_openai, state)  # type: ignore[arg-type]

        out = engine.retrieve("anything")
        assert [s.chunk_id for s in out] == ["b1"], (
            "retrieve() must keep the last surviving source rather than "
            "returning an empty list"
        )
