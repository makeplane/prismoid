# backend/tests

The backend's tests. Run everything from `backend/`. The rules for writing a test are in
[CLAUDE.md](CLAUDE.md); this page says what runs where.

## Tiers

| Folder | What it holds | Needs | Run |
| --- | --- | --- | --- |
| `unit/` | Fast tests, one folder per package under test | nothing | `just test-unit` (also in `just check`) |
| `laws/` | One file per standing rule (ADR-075) | nothing | `just test-unit` |
| `integration/` | Tests by product domain against real services | the compose stack (`just up`) | `just test-integration` |
| `conformance/` | Clauses every frame driver and model row must pass | the compose stack | `just test-integration` |
| `contract/` | One real turn per scenario, held to a recorded shape | the compose stack | `PRISMOID_TEST_FRESH_DB=1 uv run pytest tests/contract -n 6` |
| `drills/` | Call-log durability drills (SIGKILL, resume) | the compose stack | `uv run pytest tests/drills -m drill -n 4` |
| `perf/` | The HANDOFF §15 budgets | the running API | `just perf` |

`just test` runs the default selection across every tier, in parallel. `just device-proof` runs the
macOS device proofs in `integration/device/` and fails any test this Mac cannot run.

## Where a new test goes

1. Needs no service: `unit/`, in the folder that mirrors the code under test
   (`prismoid/ai/tools/edit.py` -> `unit/ai/tools/`). See [unit/README.md](unit/README.md).
2. Needs Postgres, Silo, Docker or a Mac: `integration/`, in the folder of the product domain it
   proves. See [integration/README.md](integration/README.md).
3. A rule over the whole codebase: one file in `laws/`. See [laws/README.md](laws/README.md).
4. A clause every driver must pass: `conformance/`. A turn's recorded shape: `contract/`.

Name it `test_<what it tests>.py` with a basename no other test file uses, and add `__init__.py` to a
new folder (`perf/` alone has none).

## Shared code and data

- `support/`: plain modules tests import (`fakes/`, `rigs/`, `paths.py`, `markers.py`, `cast.py`, ...).
  See [support/README.md](support/README.md). A test never imports another test module or `tests.conftest`.
- `conftest.py`: fixtures and hooks only (the per-worker database clone, the time-windowed cleanup).
  `integration/conftest.py` holds `db`, `actor` and `client`.
- `fixtures/`: data files tests read. `snapshots/`: the recorded model requests
  `unit/ai/agents/test_request_snapshots.py` compares against.

## Markers

The default run deselects `perf`, `live` and `drill` (`pyproject.toml`).

| Marker | Meaning |
| --- | --- |
| `integration` | needs the compose stack |
| `frames` | spins real frames; set from the `frames-*` xdist groups in `conftest.py` |
| `live` | calls a real model and costs money; only with `-m live` |
| `drill` | a durability drill; only with `-m drill` |
| `perf` | a performance budget; only with `-m perf` |
| `must_run` | under `PRISMOID_DEVICE_PROOF`, fails a test this machine cannot run (`support/markers.py`) |

Evals, which score how well the agents decide, live in [backend/evals](../evals/README.md).
