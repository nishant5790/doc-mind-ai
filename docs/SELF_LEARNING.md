# Self-Learning System Documentation

## Overview

The self-learning system is a three-layer feedback loop that continuously improves the RAG (Retrieval-Augmented Generation) pipeline based on user interactions. Users provide explicit feedback (👍/👎) with optional corrections, which are processed hourly to refine chunk quality scores, distill learned rules, and build a golden Q&A corpus.

## Architecture

### Components

1. **Feedback Collection** — User feedback (thumbs up/down + optional correction text)
2. **Learning Loop** — Hourly batch processor that runs three improvement layers
3. **Cosmos DB Storage** — Persists feedback, learned rules, golden pairs, and chunk quality metrics
4. **RAG Query Pipeline** — Injects learned artifacts into prompts at query time

### Data Flow

```
User Feedback (👍/👎 + correction)
    ↓
Cosmos DB Feedback Container
    ↓
Worker Process (Hourly LearningLoop.run_once())
    ├── Layer 1: Chunk Quality Updates
    ├── Layer 2: Rule Distillation
    └── Layer 3: Golden Pair Promotion
    ↓
Learned Artifacts (Quality Scores, Rules, Pairs)
    ↓
RAG Pipeline (Next Query)
    ├── Rerank chunks by quality_score
    ├── Inject rules into system prompt
    └── Add golden pairs as few-shot examples
```

## Three Learning Layers

### Layer 1: Chunk Quality Scoring

**What it does:** Tracks which retrieved chunks appear in good vs. bad answers.

**Mechanism:**
- For each feedback record, increment `times_in_good_answer` or `times_in_bad_answer` on all cited chunks
- Recalculate: `quality_score = times_in_good_answer / (times_in_good_answer + times_in_bad_answer)`
- Default score: `0.5` for unseen chunks

**Impact on RAG:**
- Retrieved chunks are re-ranked by `(quality_score, original_score)` tuple
- High-quality chunks float to the top of the context window

**Code Location:** `src/learning.py:_update_chunk_quality()`

---

### Layer 2: Learned Rules (Rule Distillation)

**What it does:** Converts user corrections into imperative guidelines for the assistant.

**Mechanism:**
1. Collect last 30 feedback records with `rating="down"` (👎) and a `correction` string
2. Build a prompt with patterns:
   ```
   Q: {question}
   Bad answer: {answer}
   Correction: {correction}
   ```
3. Call GPT-4o (temperature=0.0) with `DISTIL_PROMPT` to extract 3–7 rules in strict JSON
4. Save each `LearnedRule` with metadata:
   - `category`: e.g., "general"
   - `rule`: imperative string (e.g., "Always cite sources for claims")
   - `evidence_count`: number of corrections used
   - `updated_at`: ISO timestamp

**Impact on RAG:**
- Top 5 rules pulled at query time
- Appended to system prompt under "Learned guidelines (from past corrections):"
- Guides assistant behavior without retraining

**Code Location:** `src/learning.py:_distil_rules()`

---

### Layer 3: Golden Q&A Pairs

**What it does:** Saves high-quality question-answer turns as few-shot examples.

**Mechanism:**
1. For each feedback with `rating="up"` (👍), `question`, and `answer`, upsert as `GoldenPair`
2. Store with:
   - `topic`: e.g., "general"
   - `question`: user's query
   - `answer`: assistant's response
   - `chunk_ids`: supporting chunks

**Impact on RAG:**
- Top 2 pairs fetched at query time
- Injected into system prompt as few-shot examples
- Demonstrates desired answer format and style

**Code Location:** `src/learning.py:_promote_golden()`

---

## Refresh Schedule

### Worker Loop

Located in `worker.py`:

```python
POLL_INTERVAL_SECONDS = 5      # Check for ingestion tasks every 5 seconds
LEARN_INTERVAL_SECONDS = 3600  # Run learning loop every hour
```

### Update Mechanism

- Worker continuously polls for ingestion tasks
- Every 3,600 seconds (1 hour), triggers `learner.run_once()`
- `run_once()` processes the last 200 feedback records
- All three layers run in a single batch
- Results are upserted to Cosmos DB immediately

### No Caching

The RAG pipeline queries `get_rules()`, `get_golden_pairs()`, and `get_chunk_quality()` on **every user query**. There is no caching layer, so:
- ✅ New rules are live within seconds of being saved
- ✅ No service restart required
- ✅ Quality updates apply immediately

---

## How to Evaluate Self-Learning

### 1. **Monitor Learning Statistics**

Check the hourly output from `LearningLoop.run_once()`:

```python
stats = {
    "feedback_count": int,      # How many feedback records processed
    "rules_added": int,         # New rules created in this run
    "golden_added": int,        # New golden pairs saved
    "chunk_updates": int,       # Chunk quality increments
}
```

**Where to check:**
- Cosmos DB container `learning_stats` (if logged)
- Application logs from the worker process
- Add telemetry by extending `worker.py` to emit metrics

---

### 2. **Track Chunk Quality Distribution**

Query Cosmos DB `chunk_quality` container to see score evolution:

```sql
SELECT 
    chunk_id,
    times_in_good_answer,
    times_in_bad_answer,
    quality_score,
    updated_at
FROM chunk_quality
ORDER BY quality_score DESC
```

**Good metrics:**
- Most chunks cluster around `0.5` (unbiased) initially
- Over time, chunks polarize: high-quality → `0.8+`, low-quality → `<0.3`
- Significant spread indicates learning is differentiating

---

### 3. **Audit Distilled Rules**

Query `learned_rules` to review what the system has learned:

```sql
SELECT rule, evidence_count, updated_at
FROM learned_rules
WHERE category = 'general'
ORDER BY updated_at DESC
```

**Evaluation questions:**
- Are rules specific and actionable? (Good: "Cite sources for numerical claims." Bad: "Be better.")
- Do rules reflect common correction patterns?
- Are there duplicates or contradictions?

**Manual review:**
- Compare rules to the correction corpus in `feedback` container
- Ensure distillation is faithful to user intent

---

### 4. **Compare Answer Quality (Before/After)**

**Pre-learning baseline:**
- Turn off the learning system (or query without using `get_rules()` and `get_golden_pairs()`)
- Run N test questions
- Score answers on: relevance, correctness, source citation

**Post-learning:**
- Re-run same N test questions after learning has accumulated feedback
- Compare metrics:
  - ✓ Relevance score improvement
  - ✓ Citation accuracy
  - ✓ Adherence to learned rules
  - ✓ Token efficiency (fewer hallucinations = shorter answers)

---

### 5. **Feedback Loop Health**

Monitor feedback input quality:

```sql
SELECT 
    COUNT(*) as total_feedback,
    SUM(CASE WHEN rating = 'up' THEN 1 ELSE 0 END) as thumbs_up,
    SUM(CASE WHEN rating = 'down' AND correction IS NOT NULL THEN 1 ELSE 0 END) as corrections,
    DATEDIFF(hour, MIN(created_at), MAX(created_at)) as hours_span
FROM feedback
```

**Healthy system signs:**
- Feedback volume > 10–20 per day (or per learning cycle)
- Ratio of corrections to thumbs-down > 30%
- Even distribution of positive/negative feedback

---

### 6. **Golden Pairs Effectiveness**

Monitor if golden pairs are being used and helpful:

```sql
SELECT topic, COUNT(*) as count, MAX(created_at) as latest
FROM golden_pairs
GROUP BY topic
```

**Manual audit:**
- Sample 5–10 random golden pairs
- Check if they represent genuinely good answers
- Verify questions are representative of common user queries

---

### 7. **Chunk Quality Re-ranking Impact**

Measure if quality-scored chunks improve retrieval:

**Test scenario:**
1. Query with learning disabled (quality_score ignored)
2. Retrieve top-5 chunks by semantic similarity
3. Note if top-1 chunk is relevant

4. Query with learning enabled (quality_score re-ranks)
5. Retrieve top-5 chunks by `(quality_score, similarity)` tuple
6. Note if top-1 chunk is relevant

**Compare:**
- % of queries where top chunk improved
- Average relevance rank before/after

---

## Evaluation Dashboard Template

Create a dashboard or report tracking these metrics over time:

| Metric | Baseline | Week 1 | Week 2 | Trend |
|--------|----------|--------|--------|-------|
| Avg Answer Relevance | 0.72 | 0.75 | 0.78 | ↑ |
| Rules Generated | 0 | 8 | 15 | ↑ |
| Golden Pairs | 0 | 12 | 25 | ↑ |
| Avg Chunk Quality Score | 0.50 | 0.54 | 0.58 | ↑ |
| Feedback Volume | 0 | 45 | 120 | ↑ |
| User 👍 Rate | N/A | 60% | 68% | ↑ |

---

## Reset & Testing

### Wipe Learning State

To start fresh or test the learning system:

```python
from src.cosmos_client import create_state_service

cosmos = create_state_service()
cosmos.wipe_learning()  # Clears: feedback, learned_rules, golden_pairs, chunk_quality
```

**Use cases:**
- Before running A/B tests
- Resetting demos
- Testing rule distillation on new feedback corpus

---

## Implementation Notes

- **Language:** Python (async-safe with `cosmos_client.CosmosService`)
- **Storage:** Cosmos DB (NoSQL)
- **LLM:** GPT-4o for rule distillation
- **Schedule:** Configurable (default 1 hour)
- **Scalability:** Worker process is independent; can be replicated without conflict (Cosmos ensures consistency)

---

## Troubleshooting

| Issue | Cause | Solution |
|-------|-------|----------|
| Rules not being generated | No corrections in feedback | Ensure users provide correction text on 👎 |
| Quality scores stay at 0.5 | Chunks not cited in feedback | Check that chunk_ids are correctly tracked in feedback |
| Golden pairs not improving answers | Low-quality examples being saved | Add quality gate: only save if feedback has high confidence |
| Learning loop crashes | JSON parsing error in GPT response | Add retry logic and fallback to empty rules |
