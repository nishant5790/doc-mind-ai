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
    "Answer ONLY using the provided context. "
    "If the answer is not in the context, say so plainly. "
    "Always cite sources by referring to the page number, e.g. (page 4). "
    "If the question is about a diagram, figure, chart, screenshot, or any "
    "visual element, prefer context chunks where type=image and explicitly "
    "mention them in your answer (e.g. \"see the figure on page 4\") so the "
    "UI can display the relevant image alongside the answer. "
    "Be concise."
)


class RAGEngine:
    def __init__(
        self,
        search: SearchService,
        openai: OpenAIService,
        cosmos: CosmosService,
        top_k: int = 5,
    ) -> None:
        self.search = search
        self.openai = openai
        self.cosmos = cosmos
        self.top_k = top_k

    # ------------------------------------------------------------------
    def retrieve(self, question: str, doc_ids: Optional[list[str]] = None) -> list[Source]:
        embedding = self.openai.embed(question)[0]
        sources = self.search.search(question, embedding, top_k=self.top_k, doc_ids=doc_ids)

        # Always pull a couple of best-matching image chunks so the UI can
        # render relevant figures/diagrams alongside the answer. Image chunks
        # carry only a short description and are easily out-ranked by text
        # chunks in hybrid search, so we fetch them on a separate pass.
        VISUAL_HINTS = (
            "diagram", "figure", "chart", "image", "picture", "screenshot",
            "graph", "flow", "architecture", "schematic", "illustration",
            "drawing", "visual", "show", "display",
        )
        has_image = any(s.type == "image" for s in sources)
        wants_visual = any(w in question.lower() for w in VISUAL_HINTS)
        if wants_visual or not has_image:
            try:
                img_sources = self.search.search(
                    question,
                    embedding,
                    top_k=3 if wants_visual else 2,
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

        # Re-rank by chunk quality score from Cosmos (cheap O(top_k) lookup).
        for s in sources:
            cq = self.cosmos.get_chunk_quality(s.chunk_id)
            if cq:
                self.cosmos.update_chunk_quality(s.chunk_id, retrieved=True)
        sources.sort(
            key=lambda s: (self.cosmos.get_chunk_quality(s.chunk_id).quality_score
                           if self.cosmos.get_chunk_quality(s.chunk_id) else 0.5),
            reverse=True,
        )
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
        # Last few turns
        for t in history[-6:]:
            msgs.append({"role": t.role, "content": t.content})
        msgs.append(
            {
                "role": "user",
                "content": f"Context:\n{context_block}\n\nQuestion: {question}",
            }
        )
        return msgs

    # ------------------------------------------------------------------
    def answer(
        self, question: str, session_id: str, user_id: str = "anonymous", doc_ids: Optional[list[str]] = None
    ) -> tuple[str, list[Source], ChatTurn]:
        sources = self.retrieve(question, doc_ids=doc_ids)
        history = self.cosmos.get_history(session_id)
        messages = self.build_messages(question, sources, history)

        # Persist user turn first
        user_turn = ChatTurn(session_id=session_id, user_id=user_id, role="user", content=question)
        self.cosmos.save_turn(user_turn)

        answer_text = self.openai.chat(messages)
        assistant_turn = ChatTurn(
            session_id=session_id,
            user_id=user_id,
            role="assistant",
            content=answer_text,
            sources=sources,
        )
        self.cosmos.save_turn(assistant_turn)
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
        history = self.cosmos.get_history(session_id)
        messages = self.build_messages(question, sources, history)

        user_turn = ChatTurn(session_id=session_id, user_id=user_id, role="user", content=question)
        self.cosmos.save_turn(user_turn)

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
        self.cosmos.save_turn(assistant_turn)
        yield {"type": "done", "turn_id": assistant_turn.id}
