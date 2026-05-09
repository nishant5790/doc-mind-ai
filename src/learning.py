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

import hashlib
import json
import logging
import re
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
    # Words that strongly suggest a 👎 + correction is about *answer style*
    # (formatting, length, tone) rather than the chunks themselves being
    # wrong. We use them to skip the chunk-quality downgrade so that asking
    # "format this as bullets" doesn't permanently retire the source chunks.
    _STYLE_CORRECTION_HINTS = (
        "bullet", "bulleted", "bullet point", "bullet points",
        "format", "formatting", "structure", "structured",
        "concise", "shorter", "longer", "detail", "detailed", "in detail",
        "tone", "polite", "friendly", "language", "rewrite", "reword",
        "table", "list", "paragraph", "headings", "heading", "step by step",
        "numbered", "summary", "summarise", "summarize",
    )
    _REMOVAL_INTENT_HINTS = (
        "remove", "don't show", "do not show", "dont show",
        "don't include", "do not include", "dont include",
        "unnecessary", "unneccessary", "irrelevant", "unrelated",
        "not needed", "not relevant", "drop", "hide", "exclude",
        "shouldn't reference", "should not reference",
    )
    # Map user-facing modality words to ChunkRecord.type values.
    _MODALITY_WORDS = {
        "image": "image", "images": "image",
        "figure": "image", "figures": "image",
        "diagram": "image", "diagrams": "image",
        "chart": "image", "charts": "image",
        "picture": "image", "pictures": "image",
        "screenshot": "image", "screenshots": "image",
        "photo": "image", "photos": "image",
        "table": "table", "tables": "table",
        "text": "text", "paragraph": "text", "paragraphs": "text",
    }

    @classmethod
    def _is_style_only_correction(cls, correction: str) -> bool:
        if not correction:
            return False
        c = correction.lower()
        if any(h in c for h in cls._REMOVAL_INTENT_HINTS):
            # Removal intent dominates — even if it also mentions a
            # style-ish word, treat it as a content correction.
            return False
        return any(h in c for h in cls._STYLE_CORRECTION_HINTS)

    @classmethod
    def _removal_target_types(cls, correction: str) -> set[str]:
        """Return chunk types the user explicitly asked to drop, or empty
        set if the correction has no removal intent.

        E.g. 'remove the image on page 11 that is unnecessary' -> {'image'}.
        """
        if not correction:
            return set()
        c = correction.lower()
        if not any(h in c for h in cls._REMOVAL_INTENT_HINTS):
            return set()
        targets: set[str] = set()
        for word, modality in cls._MODALITY_WORDS.items():
            if re.search(rf"\b{re.escape(word)}\b", c):
                targets.add(modality)
        return targets

    # ------------------------------------------------------------------
    def _update_chunk_quality(self, feedback: list[FeedbackRecord]) -> int:
        n = 0
        for fb in feedback:
            correction = fb.correction or ""
            # 1) Style-only correction — chunks were fine, only formatting
            #    was off. Don't penalize anything.
            if fb.rating == "down" and self._is_style_only_correction(correction):
                log.info(
                    "Skipping chunk-quality downgrade for style-only "
                    "correction on feedback %s",
                    getattr(fb, "id", "?"),
                )
                continue

            # 2) Targeted removal correction (e.g. 'remove the image on
            #    page 6 that is unnecessary') — only penalize chunks whose
            #    type matches the removed modality. We need chunk_meta for
            #    this; if the feedback row pre-dates that field we fall
            #    back to penalizing all cited chunks.
            target_types: set[str] = set()
            if fb.rating == "down" and fb.chunk_meta:
                target_types = self._removal_target_types(correction)

            for cid in fb.chunk_ids:
                if fb.rating == "up":
                    self.cosmos.update_chunk_quality(cid, good=True)
                    n += 1
                    continue

                if target_types:
                    meta = next(
                        (m for m in fb.chunk_meta if m.chunk_id == cid),
                        None,
                    )
                    if meta and meta.type not in target_types:
                        # User asked to drop a different modality — leave
                        # this chunk's quality alone.
                        continue
                    # Match (or no meta): apply a stronger negative
                    # signal so a single explicit removal request is
                    # enough to retire the offending chunk.
                    self.cosmos.update_chunk_quality(cid, bad=True)
                    self.cosmos.update_chunk_quality(cid, bad=True)
                    n += 2
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

        added = 0
        seen_ids: set[str] = set()
        for r in rules:
            text = r.strip()
            if not text:
                continue
            norm = self._normalize_rule(text)
            if not norm:
                continue
            category = "general"
            rule_id = self._rule_id(category, norm)
            if rule_id in seen_ids:
                continue
            seen_ids.add(rule_id)
            self.cosmos.save_rule(
                LearnedRule(
                    id=rule_id,
                    category=category,
                    rule=text,
                    evidence_count=len(corrections),
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
            )
            added += 1
        return added

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
    def _normalize_rule(text: str) -> str:
        """Normalize a rule for de-duplication.

        Lowercase, strip, collapse whitespace, drop trailing punctuation, and
        remove non-alphanumeric noise so near-identical wordings collapse to
        the same key.
        """
        t = text.lower().strip()
        t = re.sub(r"\s+", " ", t)
        t = re.sub(r"[^a-z0-9 ]+", "", t)
        return t.strip()

    @staticmethod
    def _rule_id(category: str, normalized_rule: str) -> str:
        h = hashlib.sha1(f"{category}:{normalized_rule}".encode("utf-8")).hexdigest()
        return f"rule-{h[:24]}"

    # ------------------------------------------------------------------
    @staticmethod
    def _strip_fences(text: str) -> str:
        t = text.strip()
        if t.startswith("```"):
            t = t.split("\n", 1)[1] if "\n" in t else t
            if t.endswith("```"):
                t = t.rsplit("```", 1)[0]
        return t.strip()
