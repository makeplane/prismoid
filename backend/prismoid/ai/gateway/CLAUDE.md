# ai/gateway - folder rules
Scope: `backend/prismoid/ai/gateway/`. Narrows ../CLAUDE.md and the root CLAUDE.md; never contradicts them.

## Purpose
Turning a `ModelClass` into a metered model with fallbacks, and pricing what runs cost. Catalog rows,
routing tables and price books are rows that `operations/models.py`, `operations/routing.py` and
`operations/prices.py` load and pass in; the starter data is `runner/default_models.yaml`.

## What is here
- `classes.py`: one catalog row as a value (`ModelSpec`, prices by native `Unit`).
- `resolve.py`: `resolve()` (the chain), `chain_for` (routing order, `exclude_family`, the quality dial),
  `judge_seats`, `StickyFallback`.
- `meter.py`: `MeteredModel`, one usage record per provider call, and the flex tier.
- `providers.py`: one constructor per wire protocol, `FEATURES`, `ClassSettings` (effort, service tier),
  per-class timeouts, cache settings and keys (`api_key_for`).
- `throttle.py`: one limiter and one retrying client per provider (`fails_over` decides a failover).
- `run_cost.py`: a run's projected cost - the Lead's estimate and the budget wave both read it.
- `conformance.py`: the conformance check behind `prismoid models evaluate`.

## Where a thing goes
- A new wire protocol: one constructor in `providers.py` and its `FEATURES` row.
- A new model: catalog data (a row and its prices), never code here.
- Not here: a search engine or image API -> `ai/tools/providers/`; a catalog or price row -> `operations/`.

## Rules
- Speak `ModelClass` to everyone else; a model id arrives as data on a row, never as a literal here.
- Keep the wrap order: limiter outside the meter, meter inside each fallback candidate - queue wait is
  never billed, and the row that answered is the row billed.
- Send a setting only to a row that prices it: the 1-hour cache and `service_tier: flex` are stripped for
  rows without those prices (`MeteredModel._for_row`).
- Ask any protocol question through `FEATURES` / `features()`, nothing else.
- Put every projected cost in `run_cost.py`, so an estimate and a budget never disagree.
- Retry the same model on the transport first; fail over to the next row only on `throttle.fails_over` -
  a retry is a pause, a new row changes who wrote the answer.
- Never merge units or guess a price: an unpriced unit costs 0, so the hole shows in the ledger.
- Never pass a quality dial to the judge: `judge_seats` orders by distance from the maker, not cost.
- Never read a key from a catalog row: keys are deployment settings (`api_key_for`).

## Checked by machine
- `.importlinter`: `gateway` is a bottom-line sibling of `frames | skills | tools | bots`.
- `tests/laws/test_law_model_features.py` (protocol questions only through `FEATURES`),
  `tests/laws/test_law_routed_seats.py` (every seat passes the routing order),
  `tests/laws/test_law_agent_construction.py` (`conformance.py` is the one drill outside `agents/`).
- `backend/tools/audit.sh`: no quoted model string outside a `providers/` directory.

## Verify
`uv run pytest tests/unit/ai/gateway -q`; a live row, metered: `prismoid models evaluate <provider:model_id>`.
