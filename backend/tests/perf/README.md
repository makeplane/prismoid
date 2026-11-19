# tests/perf

The performance budgets (HANDOFF §15) as tests. Every file carries the `perf` marker, which the
default run deselects.

- `test_budgets.py`: warm p95 of the request path (list, login, capture, enqueue) against the running
  API; skipped when the API is unreachable. Run with `just perf`.
- `test_routine_budgets.py`: the routine sweep and mint floors, straight on Postgres (also marked
  `integration`); skipped when the database is unreachable.

Add a budget here only for a floor a test can time without a model; budgets that need real model work
belong in the smoke and load drills. This folder has no `__init__.py`, so keep its basenames unique.

All tiers and how to run them: [tests/README.md](../README.md).
