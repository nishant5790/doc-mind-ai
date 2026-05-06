"""
src/learning.py — Self-improvement loop.

Three layers of learning, all driven by user feedback:

* **Layer 1 — Explicit corrections → learned rules.**
  Aggregate `correction` strings from the feedback container and ask
  gpt-4o to distil them into a small set of imperative guidelines that
  get injected into the system prompt at query time.

* **Layer 2 — Implicit feedback → chunk-quality scores.**
  Increment `times_in_good_answer` / `times_in_bad_answer` on the
  chunks cited by a 👍 / 👎 feedback. The RAG engine re-ranks future
  retrievals using these scores.

* **Layer 3 — Golden Q&A pairs.**
  Promote 👍-rated turns into `golden_pairs` so they can be injected
  as few-shot examples in future prompts.

This module exposes one entry point — `LearningLoop.run_once()` — that
the worker calls on a schedule.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from src.cosmos_client import CosmosService
from src.models import FeedbackRecord, GoldenPair, LearnedRule
from src.openai_client import OpenAIService

log = logging.getLogger(__name__)


DISTIL_PROMPT = (
    "You are reviewing user corrections of a Q&A assistant. "
    "From the corrections below, extract 3-7 short imperative guidelines "
    "the assistant should follow next time. Return STRICT JSON: "
    '{"rules": ["rule 1", "rule 2", ...]}.\n\n'
)


class LearningLoop:
    def __init__(self, cosmos: CosmosService, openai: OpenAIService) -> None:
        self.cosmos = cosmos
        self.openai = openai

    # ------------------------------------------------------------------
    def run_once(self) -> dict:
        """Run all three learning layers. Returns a stats summary."""
        feedback = self.cosmos.list_feedback(limit=200)
        stats = {
            "feedback_count": len(feedback),
            "rules_added": 0,
            "golden_added": 0,
            "chunk_updates": 0,
        }
        if not feedback:
            return stats

        stats["chunk_updates"] = self._update_chunk_quality(feedback)
        stats["rules_added"] = self._distil_rules(feedback)
        stats["golden_added"] = self._promote_golden(feedback)
        log.info("Learning loop done: %s", stats)
        return stats

    # ------------------------------------------------------------------
    def _update_chunk_quality(self, feedback: list[FeedbackRecord]) -> int:
        n = 0
        for fb in feedback:
            for cid in fb.chunk_ids:
                if fb.rating == "up":
                    self.cosmos.update_chunk_quality(cid, good=True)
                else:
                    self.cosmos.update_chunk_quality(cid, bad=True)
                n += 1
        return n

    # ------------------------------------------------------------------
    def _distil_rules(self, feedback: list[FeedbackRecord]) -> int:
        corrections = [
            f"Q: {f.question}\nBad answer: {f.answer}\nCorrection: {f.correction}"
            for f in feedback
            if f.rating == "down" and f.correction
        ]
        if not corrections:
            return 0

        prompt = DISTIL_PROMPT + "\n---\n".join(corrections[-30:])
        try:
            content = self.openai.chat(
                [
                    {"role": "system", "content": "You output strict JSON only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=400,
            )
            data = json.loads(self._strip_fences(content))
            rules: list[str] = data.get("rules", [])
        except Exception as e:
            log.warning("Rule distillation failed: %s", e)
            return 0

        for r in rules:
            self.cosmos.save_rule(
                LearnedRule(
                    category="general",
                    rule=r.strip(),
                    evidence_count=len(corrections),
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
            )
        return len(rules)

    # ------------------------------------------------------------------
    def _promote_golden(self, feedback: list[FeedbackRecord]) -> int:
        n = 0
        for fb in feedback:
            if fb.rating == "up" and fb.question and fb.answer:
                self.cosmos.save_golden(
                    GoldenPair(
                        topic="general",
                        question=fb.question,
                        answer=fb.answer,
                        chunk_ids=fb.chunk_ids,
                    )
                )
                n += 1
        return n

    # ------------------------------------------------------------------
    @staticmethod
    def _strip_fences(text: str) -> str:
        t = text.strip()
        if t.startswith("```"):
            t = t.split("\n", 1)[1] if "\n" in t else t
            if t.endswith("```"):
                t = t.rsplit("```", 1)[0]
        return t.strip()
