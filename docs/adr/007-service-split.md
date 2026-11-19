# ADR-007 — Service split: the backend becomes `backend/`, the web app stays `web/`

*Status: accepted · July 2026 · stage m10*

## Context

Through m9 the repository was the backend: the Python project sat at the root (`src/computer/`,
`migrations/`, `tests/`, `tools/`, `evals/`, `skills/`, `pyproject.toml`, `alembic.ini`, `Dockerfile`,
`deploy/`), and the frontend landed later as one directory (`web/`, the F-stages). That left one
service owning the repo root and the other renting a corner of it — workable, but the wrong shape the
moment two services exist: root-level tooling reads as repo-wide when it is backend-only, and every
new top-level file has to answer "whose are you?"

The owner's requirement (July 2026): **a clean separation between the backend service and the
content/web service.** The backend is a service named `backend`; everything backend-related —
migrations included — lives inside it; background work stays in one place.

## Decision

Two services, one repo, repo-wide things at the root and nothing else:

```
computer/
├── HANDOFF.md  CLAUDE.md  README.md  justfile        # governance + the one build harness
├── .github/workflows/                                # CI (runs the backend from backend/)
├── docs/                                             # cross-service: adr, logs (M* and web-F*),
│   └── api/                                          #   runbooks, and the frozen API contract
├── backend/                                          # ═ THE BACKEND SERVICE ═
│   ├── computer/                                     # the package (src/ layer dropped; import
│   │   ├── conf.py runner/ schemas/ api/             #   name stays `computer.*`, Rule 11)
│   │   ├── db/
│   │   │   ├── engine.py session.py base.py models/
│   │   │   └── migrations/                           # Alembic env.py + versions/ — schema history
│   │   │                                             #   lives WITH the db layer it migrates
│   │   ├── operations/ jobs/ ai/ features/ testutils/
│   ├── tests/  tools/  evals/  skills/  skills-work/
│   ├── deploy/compose.yaml                           # the backend dev/CI stack
│   ├── pyproject.toml  uv.lock  alembic.ini  justfile-less (root drives it)
│   ├── Dockerfile  .dockerignore  .importlinter  locustfile.py
│   └── .env  .env.example  .python-version  README.md
└── web/                                              # ═ THE CONTENT/WEB SERVICE ═ (unchanged)
```

- **The package import name does not change.** `computer.*` is load-bearing: Alembic version
  scripts, DBOS's recovery journal (workflow module paths in Postgres), the import-linter
  contracts, and every absolute import keep working untouched. Only the *directory* moves:
  `src/computer/` → `backend/computer/` (flat layout — an application service, not a published
  library, does not need the src/ indirection).
- **Migrations live with the db layer: `backend/computer/db/migrations/`** (the owner's call:
  schema history next to the models it migrates, Django-style). `alembic.ini` moves to `backend/`
  (so bare `alembic` works from the service dir) and points `script_location` there. Migrations
  now ship inside the package — the production image can migrate itself. The `alembic_version`
  table, revision ids, and history are untouched.
- **Background work is one place, inside the service:** `backend/computer/jobs/` (every job, per
  Rule 2) with the DBOS worker boot in `backend/computer/runner/worker.py` — all of it now under
  `backend/`.
- **`docs/` stays at the root.** It is genuinely cross-service: the M-stage logs (backend), the
  web-F logs (web), the ADRs, and `docs/api/` — the frozen OpenAPI + event schema that `web`
  generates its types from (`pnpm gen` reads `../docs/api/`). Splitting it would fork the paper
  trail; the contract files are the two services' shared boundary and belong to neither.
- **The `justfile` stays at the root** with `set working-directory := 'backend'`: every recorded
  command in nine stages of logs (`just up`, `just migrate`, `just validate m0 …`) keeps working
  verbatim from anywhere in the repo, and `just` remains the one harness for both services.
- **CI workflows** gain `defaults.run.working-directory: backend`; artifact/cache paths become
  `backend/…` (those resolve from the workspace root, not the step working directory).

## What deliberately did not move

`HANDOFF.md`, `CLAUDE.md` (governance), `.github/` (repo-level CI), `docs/` (above), `web/`
(already a clean service), `.gitignore` and `.pre-commit-config.yaml` (both operate repo-wide;
pre-commit's local hooks cd into `backend/`).

## Consequences

- Deploys/scripts outside this repo that assumed the root layout must switch to
  `docker build backend/` (context is now the service dir — the image no longer swallows `web/`)
  and `cd backend` for `uv`/`alembic` invocations. `tools/fresh_clone.sh` documents the new
  bring-up and stays the stopwatch.
- CWD-relative runtime paths were re-pointed once: `perf_baseline_path` and the contract
  exporter's output now cross to the root `docs/` (`../docs/…`) — they are the only two places
  the backend reaches outside its directory, both tooling-side, neither at serving runtime.
  (Amended 2026-11-12: the perf baseline moved to `backend/tests/perf/baseline.json`, beside the
  harness that reads it, so `perf_baseline_path` no longer leaves `backend/`; the contract
  exporter is the one crossing left.)
- One depth change: `operations/skills.py` resolves the builtin `skills/` dir relative to the
  package file; dropping `src/` shortens that walk by one parent.
- Rollback is `git revert` of the m10 commits (moves are pure `git mv` + surgical edits; no
  history rewrite, tags m0–m9 untouched).

## Validation invariant

The reshape must be **behavior-invisible**: the full gate (`just check`), the whole test suite,
the accumulated stage replays (`just validate m0 … m9`), and a byte-identical regeneration of the
frozen contract (`just export-contract` → empty `git diff` on `docs/api/`) all pass from the new
layout before the stage tags.

> *Amended by ADR-008 (2026-07-22): the accumulated stage replays are retired. The reshape landed
> gated on `just check` + the full suite (475 passed) + byte-identical `just export-contract` +
> `just smoke` — commits `89aaeca` (pure move) and `a5fa620` (re-point).*
