# ADR-011 — Memory retrieval: FTS now, the pgvector hybrid pre-planned

*Status: **deferred with triggers** · July 2026 · companion to the memory & context subsystem*

## Context

The context ledger (memories + `context.assembled`) needs retrieval: which of a scope's
memories belong in a task's pack. Two facts bound the problem. First, every scope is
**capped at 200 active rows** (`ACTIVE_CAP`, LRU-retired), so retrieval selects from
hundreds of short sentences, not millions of documents — the per-lane token budget is the
real filter, not recall. Second, the platform's differentiator is **provenance**: every
injected memory is named in a durable event and joined in `memory_uses`. Any retrieval
engine that cannot live inside our Postgres, our transaction, and our role model breaks
that story.

## Decision (v1 — shipped)

Postgres FTS, zero new dependencies:

- `memories.text_tsv` — a stored generated column, `to_tsvector('english', text)`, GIN-indexed.
- `Memory.search()` builds a **recall-biased OR-tsquery** from the task goal (`tsquery_of`:
  lowercased words ≥3 chars, deduped, capped at 24 terms) and orders by
  `ts_rank_cd DESC, last_used_at DESC NULLS LAST, created_at DESC, id`.
- Assembly (`operations/context.assemble`) merges FTS hits with a recency list, dedupes,
  and lets the lane budget cut — deterministic, one SELECT per lane.

External memory services (mem0, Zep, Letta) were evaluated and **rejected**: the record
must live in our Postgres, inside our event stream and role model, or the provenance story
is fiction. Client-side embedding pipelines were rejected for v1 as pure cost: at ≤200
rows per scope, english-stemmed FTS with rank ordering does not observably miss.

## Known ceilings (accepted, documented in the model docstring)

- `'english'` regconfig stems English only; other languages degrade to exact-word matching.
- An OR-query is recall-biased; precision comes from rank order + the token budget.
- Cross-wording recall ("bullet points" ≈ "no long paragraphs") is beyond lexical search.

## Triggers — any ONE of these re-opens this ADR

1. Scopes regularly pressing the 200-row cap **with observed relevance misses** (the ledger's
   own `use_count`/`last_used_at` telemetry shows the right rows going unused).
2. p95 `assemble()` database time > 150 ms (§15 posture).
3. Sustained non-English usage where FTS visibly degrades.
4. A product need for cross-wording recall.

## The upgrade, pre-planned (so it is a change, not a redesign)

- Compose image `postgres:17-alpine` → `pgvector/pgvector:pg17` (`deploy/compose.yaml`).
- Migration: `memories.embedding vector(…)` + HNSW index; backfill embeddings for active rows.
- An **embedder as a catalog capability class** — metered like every provider call (Rule 8),
  so remembering keeps costing visibly.
- `Memory.search()` becomes hybrid: RRF over the FTS ranking and the ANN ranking. **Call
  sites do not change** — the seam is the classmethod, which is why it is the only query path.
- New client library (if any) = ADR acceptance + HANDOFF §3 row, per the standing rule.

## Operational notes

- Rot control is the cap + LRU retirement + `supersede-never-delete`; the ledger UI shows
  `use_count`/`last used`, which is the early-warning gauge for trigger 1.
- The archivist is one CHEAP_CLASSIFIER call per delivered task, metered as `usage.recorded`;
  runaway cost shows on the same ledger as every other spend.
- GDPR: user erasure scrubs the User satellite and must also **retire `scope=user` memories**
  (soft-delete does not fire the FK CASCADE; the erasure path is not yet built — this note
  rides with `db/models/user.py`'s).
