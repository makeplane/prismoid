# tests/laws

The one home for tests that enforce standing rules (ADR-075).

A law:
- names its rule in the docstring (the CLAUDE.md or ADR rule it enforces);
- derives its population from code (walks the tables, routes, agents), so a new
  member is covered without editing the test;
- stays a test only when it needs Python to decide. Literal greps belong in
  `tools/audit.sh`; import-direction rules belong in `.importlinter`.

All tiers and how to run them: [tests/README.md](../README.md).
