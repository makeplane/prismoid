# backend — Prismoid, the service

The digital worker's backend: FastAPI (`prismoid/api`), the DBOS worker and every background job
(`prismoid/jobs`), the engine (`prismoid/ai`), Postgres via SQLAlchemy + Alembic
(`prismoid/db`, migrations in `prismoid/db/migrations`), and the CLI (`prismoid/runner`).

Everything here runs **from this directory** — and the root `justfile` already does that for you
(`set working-directory := 'backend'`), so `just …` works from anywhere in the repo:

```
just install          # uv sync --all-groups
just up migrate       # compose stack (deploy/compose.yaml) + schema + builtin skills
just api              # the API on :8100
just worker           # the DBOS worker
just check            # the gate: lint + types + layers + audit + quality + unit tests + web typecheck
just smoke            # the whole product in one command — a demonstration, not a suite
```

The map (HANDOFF §4, reshaped by ADR-007):

```
backend/
├── prismoid/            # the package — import name `prismoid.*` (renamed by ADR-067)
│   ├── conf.py          # ONE Settings tree (PRISMOID_*), reads ./.env
│   ├── runner/          # CLI (typer) · worker boot
│   ├── db/              # rows (models/) + migrations/ — schema and its history, one home
│   ├── schemas/         # domain values
│   ├── api/             # endpoints/ serializers/ urls.py (SCREEN 1)
│   ├── operations/      # the only composer; owns transactions
│   ├── jobs/            # ═ ALL background work (jobs/all.py is SCREEN 3) ═
│   ├── ai/              # the engine — persistence-free, extractable
│   └── features/  testutils/
├── tests/  evals/  skills/  tools/
├── deploy/compose.yaml  # postgres + silo (S3) + egress-proxy (the dev/CI stack)
└── Dockerfile           # the production image: `docker build backend/`
```

Docs, ADRs, stage logs, and the frozen API contract the web app codegens from live at the repo
root (`../docs`) — they are cross-service. The web app is `../web`.
