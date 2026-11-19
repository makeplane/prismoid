# tests/contract

One real turn of the engine per scenario, its durable record held to a recorded shape.
Scenarios are data in `scenarios/` (one file per family); the machinery that runs them is in `harness/`.
Run from `backend/`: `PRISMOID_TEST_FRESH_DB=1 uv run pytest tests/contract -n 6`. Rules, layout and how to add a scenario: [CLAUDE.md](CLAUDE.md).

All tiers and how to run them: [tests/README.md](../README.md).
