"""
src/rag.py — RAG query engine.

`RAGEngine.answer()` runs:

    1. Embed the question
    2. Hybrid search (keyword + vector) in AI Search,
       optionally filtered to specific doc_ids
    3. Pull conversation history, learned rules, and golden Q&A pairs
       from Cosmos DB
    4. Build a system+context prompt (with rules and few-shot pairs)
    5. Stream the answer from gpt-4o
    6. Persist the new turn to Cosmos
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Optional

from src.cosmos_client import CosmosService
from src.models import ChatTurn, Source
from src.openai_client import OpenAIService
from src.search_client import SearchService

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are DocMind, a precise document Q&A assistant. "
    "For questions about the source documents, answer ONLY using the provided "
    "context and cite sources by page number, e.g. (page 4). If the answer is "
    "not in the context, say so plainly. "
    "However, you may also answer meta questions about THIS conversation "
    "itself (e.g. 'what did I just ask?', 'summarize our chat', 'repeat your "
    "last answer') using the prior chat messages shown to you — these do not "
    "require document context. "
    "If the question is about a diagram, figure, chart, screenshot, or any "
    "visual element, prefer context chunks where type=image and explicitly "
    "mention them in your answer (e.g. \"see the figure on page 4\") so the "
    "UI can display the relevant image alongside the answer. "
    "Be concise."
)


class RAGEngine:
    def __init__(
        self,
        azsearch: SearchService,
        openai: OpenAIService,
        cosmos: CosmosService,
        top_k: int = 5,
        memory=None,
    ) -> None:
        self.azsearch = azsearch
        self.openai = openai
        self.cosmos = cosmos
        self.top_k = top_k
        # `memory` provides save_turn / get_history. Falls back to cosmos
        # so older callers keep working.
        self.memory = memory if memory is not None else cosmos

    # Words in the user question that strongly indicate they want a visual.
    # A direct match here gives the highest confidence score (1.0). The list
    # mixes unambiguous visual nouns with weaker "process/how" hints — the
    # latter are scored lower and the LLM classifier acts as the tie-breaker.
    _VISUAL_HINTS_STRONG = (
        "diagram", "figure", "chart", "image", "picture", "screenshot",
        "graph", "flowchart", "schematic", "illustration", "drawing",
        "visual", "architecture",
    )
    _VISUAL_HINTS_WEAK = (
        "how", "workflow", "process", "flow", "pipeline", "steps",
        "stages", "sequence", "structure",
    )
    # Final score (0..1) at or above which we run the image-only search pass.
    _VISUAL_INTENT_THRESHOLD = 0.6
    # Weights blending the two signals.
    _STRONG_KEYWORD_SCORE = 1.0
    _WEAK_KEYWORD_SCORE = 0.5
    _LLM_WEIGHT = 0.7  # llm score contributes up to this much
    # Threshold below which a chunk that has received explicit feedback is
    # treated as "learned bad" and excluded from results. quality_score is
    # good/(good+bad), so a single 👎 with no 👍 yields 0.0.
    _BAD_QUALITY_THRESHOLD = 0.3

    # ------------------------------------------------------------------
    def _keyword_visual_score(self, question: str) -> float:
        """Return 1.0 for a strong visual keyword, 0.5 for a weak one, else 0."""
        q = question.lower()
        if any(w in q for w in self._VISUAL_HINTS_STRONG):
            return self._STRONG_KEYWORD_SCORE
        if any(w in q for w in self._VISUAL_HINTS_WEAK):
            return self._WEAK_KEYWORD_SCORE
        return 0.0

    # ------------------------------------------------------------------
    def _llm_visual_score(self, question: str) -> float:
        """Ask the LLM if the question wants a visual answer. Returns 0..1.

        Cheap single-token classification call. Returns 0.0 on any failure so
        retrieval never blocks on classifier issues.
        """
        try:
            msg = [
                {
                    "role": "system",
                    "content": (
                        "Classify whether answering the user's question would "
                        "benefit from showing a diagram, figure, chart, screenshot, "
                        "or other visual from the source document. "
                        "Reply with ONLY a single number from 0.0 to 1.0 — no words. "
                        "0.0 = purely textual answer; 1.0 = clearly needs a visual."
                    ),
                },
                {"role": "user", "content": question},
            ]
            raw = self.openai.chat(msg, temperature=0.0, max_tokens=5).strip()
            # tolerate stray punctuation / whitespace
            raw = raw.split()[0].rstrip(".,;:")
            score = float(raw)
            return max(0.0, min(1.0, score))
        except Exception as e:  # noqa: BLE001
            log.warning("Visual-intent LLM classification failed: %s", e)
            return 0.0

    # ------------------------------------------------------------------
    def _visual_intent_score(self, question: str) -> float:
        """Combine keyword + LLM signals into a single 0..1 score.

        Direct keyword matches dominate (strong=1.0, weak=0.5). The LLM is
        used as a *boost* — it can lift a question with no keyword over the
        threshold, and it can confirm a weak-keyword question, but it cannot
        veto a strong direct match.

        Combination is additive-with-cap so the two signals reinforce each
        other: a weak keyword + a confident LLM judgement both agreeing will
        clear the threshold, while either signal alone must be strong on its
        own to qualify.
        """
        kw = self._keyword_visual_score(question)
        if kw >= self._STRONG_KEYWORD_SCORE:
            return kw  # strong direct hit — skip the LLM call entirely
        llm = self._llm_visual_score(question) * self._LLM_WEIGHT
        return min(1.0, kw + llm)

    # ------------------------------------------------------------------
    def retrieve(self, question: str, doc_ids: Optional[list[str]] = None) -> list[Source]:
        embedding = self.openai.embed(question)[0]
        sources = self.azsearch.search(question, embedding, top_k=self.top_k, doc_ids=doc_ids)

        intent_score = self._visual_intent_score(question)
        wants_visual = intent_score >= self._VISUAL_INTENT_THRESHOLD
        log.info(
            "Visual-intent score=%.2f (threshold=%.2f) -> wants_visual=%s",
            intent_score, self._VISUAL_INTENT_THRESHOLD, wants_visual,
        )

        # Only run the image-augmentation pass when the combined signal is
        # confident enough. Keeps text-only questions free of irrelevant
        # figures while still surfacing diagrams when they're warranted.
        if wants_visual:
            try:
                img_sources = self.azsearch.search(
                    question,
                    embedding,
                    top_k=3,
                    doc_ids=doc_ids,
                    type_filter="image",
                )
                seen = {s.chunk_id for s in sources}
                for s in img_sources:
                    if s.chunk_id not in seen and s.image_url:
                        sources.append(s)
                        seen.add(s.chunk_id)
            except Exception as e:  # noqa: BLE001
                log.warning("Image-augmented search failed: %s", e)
        else:
            # Text-only question: drop image chunks that the hybrid search
            # may have returned. They confuse the UI (renders a thumbnail
            # for an unrelated diagram) and rarely help the answer.
            # BUT: only drop them if at least one non-image source remains —
            # otherwise we'd strip out the only available context (e.g. a
            # scanned PDF whose every chunk is a vision-described image).
            non_image = [s for s in sources if (s.type or "text") != "image"]
            if non_image and len(non_image) != len(sources):
                log.info(
                    "Dropped %d image source(s) on text-only question",
                    len(sources) - len(non_image),
                )
                sources = non_image
            elif not non_image and sources:
                log.info(
                    "Keeping %d image source(s) on text-only question — "
                    "no text chunks available",
                    len(sources),
                )

        # Look up quality once per chunk and reuse. Also bumps the retrieval
        # counter so we know which chunks are being shown to users.
        quality: dict[str, float] = {}
        for s in sources:
            cq = self.cosmos.get_chunk_quality(s.chunk_id)
            if cq:
                quality[s.chunk_id] = cq.quality_score
                self.cosmos.update_chunk_quality(s.chunk_id, retrieved=True)
            else:
                quality[s.chunk_id] = 0.5  # neutral default for unjudged chunks

        # Feedback-driven filter: drop chunks the system has *already learned*
        # are bad for this kind of question. We only drop chunks that have
        # received explicit feedback (good+bad > 0) — never unjudged chunks —
        # so a single 👎 is enough to retire a clearly irrelevant image.
        def _is_learned_bad(s: Source) -> bool:
            cq = self.cosmos.get_chunk_quality(s.chunk_id)
            if not cq:
                return False
            judged = cq.times_in_good_answer + cq.times_in_bad_answer
            return judged > 0 and cq.quality_score < self._BAD_QUALITY_THRESHOLD

        filtered = [s for s in sources if not _is_learned_bad(s)]
        if len(filtered) != len(sources):
            log.info(
                "Filtered %d learned-bad chunk(s) from results",
                len(sources) - len(filtered),
            )
            sources = filtered

        sources.sort(key=lambda s: quality.get(s.chunk_id, 0.5), reverse=True)
        return sources

    # ------------------------------------------------------------------
    def build_messages(
        self,
        question: str,
        sources: list[Source],
        history: list[ChatTurn],
    ) -> list[dict]:
        # Learned rules + golden pairs are injected as part of the system prompt
        rules = self.cosmos.get_rules(category="general", top=5)
        rules_block = ""
        if rules:
            rules_block = "\n\nLearned guidelines (from past corrections):\n" + "\n".join(
                f"- {r.rule}" for r in rules
            )

        golden = self.cosmos.get_golden_pairs(topic="general", top=2)
        examples_block = ""
        if golden:
            examples = "\n\n".join(f"Q: {g.question}\nA: {g.answer}" for g in golden)
            examples_block = f"\n\nReference examples of good answers:\n{examples}"

        context_block = "\n\n".join(
            f"[Source chunk_id={s.chunk_id} doc={s.doc_id} page={s.page} type={s.type}]\n{s.snippet}"
            for s in sources
        ) or "(no relevant context found)"

        system = SYSTEM_PROMPT + rules_block + examples_block

        msgs: list[dict] = [{"role": "system", "content": system}]

        # Replay prior conversation as natural user/assistant turns so the
        # model sees the dialogue exactly as it happened (no "Context:"
        # wrapper around past questions, and assistant replies are kept).
        for t in history[-6:]:
            msgs.append({"role": t.role, "content": t.content})

        # Inject retrieved context as a separate system message right before
        # the current question. Keeping it out of the user turn means past
        # turns in history stay clean and the model can still rely on the
        # latest retrieval for the new answer.
        msgs.append(
            {
                "role": "system",
                "content": f"Retrieved context for the next question:\n{context_block}",
            }
        )
        msgs.append({"role": "user", "content": question})
        return msgs

    # ------------------------------------------------------------------
    def answer(
        self, question: str, session_id: str, user_id: str = "anonymous", doc_ids: Optional[list[str]] = None
    ) -> tuple[str, list[Source], ChatTurn]:
        sources = self.retrieve(question, doc_ids=doc_ids)

        history = self.memory.get_history(session_id)
        messages = self.build_messages(question, sources, history)


        # Persist user turn first
        user_turn = ChatTurn(session_id=session_id, user_id=user_id, role="user", content=question)
        self.memory.save_turn(user_turn)

        answer_text = self.openai.chat(messages)
        assistant_turn = ChatTurn(
            session_id=session_id,
            user_id=user_id,
            role="assistant",
            content=answer_text,
            sources=sources,
        )
        self.memory.save_turn(assistant_turn)
        return answer_text, sources, assistant_turn

    # ------------------------------------------------------------------
    async def stream_answer(
        self, question: str, session_id: str, user_id: str = "anonymous", doc_ids: Optional[list[str]] = None
    ) -> AsyncIterator[dict]:
        """Async generator yielding events:
            {"type": "sources", "sources": [...]}
            {"type": "token", "content": "..."}
            {"type": "done", "turn_id": "..."}
        """
        sources = self.retrieve(question, doc_ids=doc_ids)
        history = self.memory.get_history(session_id)
        messages = self.build_messages(question, sources, history)

        user_turn = ChatTurn(session_id=session_id, user_id=user_id, role="user", content=question)
        self.memory.save_turn(user_turn)

        yield {"type": "sources", "sources": [s.model_dump() for s in sources]}

        full = ""
        async for token in self.openai.stream_chat(messages):
            full += token
            yield {"type": "token", "content": token}

        assistant_turn = ChatTurn(
            session_id=session_id,
            user_id=user_id,
            role="assistant",
            content=full,
            sources=sources,
        )
        self.memory.save_turn(assistant_turn)
        yield {"type": "done", "turn_id": assistant_turn.id}
