# tests/unit

Fast tests that need no stack: no Postgres, no Silo, no network, no real browser. They run in
`just check`, and `just test-unit` runs them with `tests/laws`.

## Where a new test goes

The folders mirror the package under test. Find the module you changed and use the same path:

| Code under test | Test folder |
| --- | --- |
| `prismoid/ai/<area>/` (agents, capabilities, context, frames, gateway, skills, tools, verification) | `unit/ai/<area>/` |
| `prismoid/ai/frames/image/`, `prismoid/ai/frames/image_browser/` (code that runs inside the sandbox) | `unit/ai/frames/image/`, `unit/ai/frames/image_browser/` |
| `prismoid/operations/`, `prismoid/operations/execution/`, `prismoid/operations/machines/` | the same path under `unit/operations/` |
| `prismoid/api/`, `db/`, `device/`, `jobs/`, `local/`, `runner/`, `schemas/` | the folder of the same name |
| `prismoid/conf*.py`, `mailer.py`, `storage*.py`, `egress_relay.py`, `observability.py` | `unit/core/` |
| `backend/evals/` | `unit/evals/` |
| `backend/tools/`, the compose and deploy files | `unit/tooling/` |

A test that needs the database or another service is an integration test: put it in
`tests/integration/` instead.

## Rules

- Name the file `test_<what it tests>.py`, with a basename no other test file uses, so
  `pytest -k` and a grep find one file.
- Every folder has an `__init__.py`. Add one when you add a folder.
- Never import another test module or `tests.conftest`. Shared fakes, rigs and helpers live in
  `tests/support/` (`fakes/` for stand-ins, `rigs/` for set-ups many files share).
- Build paths from `tests/support/paths.py` (`BACKEND`, `REPO`), never from `Path(__file__).parents[N]`.
- Keep a file under 800 lines; split it by the unit under test.

[tests/README.md](../README.md) maps every tier; `tests/CLAUDE.md` holds the full rules for the suite.
