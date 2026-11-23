# Prismoid - the build harness. `just` with no target lists everything.
#
# Two services, one harness (ADR-007): the backend lives in backend/, the web app in web/.
# Every backend recipe runs from backend/ via the line below, so recorded commands
# (`just up`, `just migrate`, `just test`) work verbatim from anywhere in the repo.
# Web recipes override it per-recipe with [working-directory: '../web'].
set working-directory := 'backend'
set shell := ["bash", "-uc"]

COMPOSE := "docker compose -f deploy/compose.yaml"

# list every target
default:
    @just --list --unsorted

# ── environment ───────────────────────────────────────────────────────────────

# install/refresh the locked toolchain
install *ARGS:
    uv sync --all-groups {{ARGS}}

# bring up postgres + the object store (Silo), then make the buckets (the init container is one-shot)
up:
    {{COMPOSE}} up -d --wait
    @{{COMPOSE}} run --rm --quiet-pull minio-init >/dev/null 2>&1 && echo "─ buckets ready ─"
    @echo "─ stack up ─"; {{COMPOSE}} ps

# stop the stack, keep the volumes
down:
    {{COMPOSE}} down

# stop the stack and destroy the data
nuke:
    {{COMPOSE}} down -v

# compose status
ps:
    {{COMPOSE}} ps

# tail the stack logs
logs *ARGS:
    {{COMPOSE}} logs -f {{ARGS}}

# psql into the app database
psql *ARGS:
    {{COMPOSE}} exec postgres psql -U prismoid -d prismoid {{ARGS}}

# ── database ──────────────────────────────────────────────────────────────────

# apply every migration (Alembic only - never create_all), then seed the builtin skills (§11)
migrate:
    uv run alembic upgrade head
    uv run prismoid skills seed

# seed the data a fresh database needs to be useful: the twelve builtin skills.
seed:
    uv run prismoid skills seed

# roll back one migration
downgrade:
    uv run alembic downgrade -1

# author a new migration
revision MSG:
    uv run alembic revision -m "{{MSG}}"

# ── run ───────────────────────────────────────────────────────────────────────

# the API process
api:
    uv run prismoid run api

# the worker process (DBOS)
worker:
    uv run prismoid run worker

# the CLI, against $PRISMOID_CLI_API_URL
cli *ARGS:
    uv run prismoid {{ARGS}}

# stop every app process - API, workers, web dev. The M10.1 anti-ghost sweep: run this
# before any start; two workers on one queue is how a week of ghosts happens.
# (Bracketed/grouped patterns so pkill/grep never match this recipe's own shell.)
stop:
    -pkill -f "prismoid run (api|worker)"
    # bin[.]c?js: react-router v8 renamed dev/bin.js → bin.cjs, and the old pattern silently
    # stopped matching - seven ghost web servers accumulated before anyone noticed (2026-08-16).
    # Scoped to this checkout's web/ so another checkout's react-router dev server is never collateral.
    # The api/worker sweep above is NOT scoped: it stops every checkout's processes, on purpose -
    # checkouts share one stack, so another checkout's worker is a second worker on the same queue.
    -pkill -f "{{justfile_directory()}}/web/node_modules/.*react-router.dev/bin[.]c?js"
    @sleep 2; ps aux | grep -E "[p]oem run|{{justfile_directory()}}/web/node_modules/.*[r]eact-router.dev/bin" \
        && echo "⚠ survivors above - kill -9 them by pid" \
        || echo "─ all app processes stopped ─"

# Targeted stops (2026-07-30 review): a backend-only iteration must not take the web dev
# server down with it - `just stop-api && just api` leaves :5173 untouched.
stop-api:
    -pkill -f "prismoid run api"

stop-worker:
    -pkill -f "prismoid run worker"

stop-web:
    # Same bin[.]c?js + repo-scoped pattern as `stop` - the bare bin[.]js half of the v8 rename
    # regression lived on here after `stop` was fixed, silently killing nothing.
    -pkill -f "{{justfile_directory()}}/web/node_modules/.*react-router.dev/bin[.]c?js"
    @sleep 1; ps aux | grep -E "{{justfile_directory()}}/web/node_modules/.*[r]eact-router.dev/bin" \
        && echo "⚠ web survivors above - kill -9 them by pid" \
        || echo "─ web dev server stopped ─"

# everything, one command: sweep old processes, ensure the stack, then API (:8100) +
# worker + web (:5173) as background daemons. Logs land in /tmp/prismoid-{api,worker,web}.log.
dev: stop up
    ( PRISMOID_ENV=dev nohup .venv/bin/prismoid run api >/tmp/prismoid-api.log 2>&1 & ) >/dev/null 2>&1
    ( PRISMOID_ENV=dev nohup .venv/bin/prismoid run worker >/tmp/prismoid-worker.log 2>&1 & ) >/dev/null 2>&1
    ( cd ../web && nohup pnpm dev >/tmp/prismoid-web.log 2>&1 & ) >/dev/null 2>&1
    @sleep 3; curl -sf --max-time 3 http://localhost:8100/healthz >/dev/null \
        && echo "api    ✓ http://localhost:8100" \
        || echo "api    … still starting - tail /tmp/prismoid-api.log"
    @echo "worker ▸ log /tmp/prismoid-worker.log"
    @echo "web    ▸ http://localhost:5173 - log /tmp/prismoid-web.log"
    @echo "─ just status to see what's running; just stop to take it all down ─"

# what's actually running - processes, listeners, health. The anti-ghost check.
status:
    @echo "── processes (want exactly one api + one worker + one web):"
    @ps aux | grep -E "[p]oem run|[r]eact-router/dev/bin" | awk '{print "  pid " $2 "  started " $9 "  " $11, $12, $13}' || echo "  none"
    @echo "── listeners:"
    @lsof -nP -iTCP:8100 -iTCP:5173 -iTCP:3001 -sTCP:LISTEN 2>/dev/null | tail -n +2 | awk '{print "  :" substr($9, index($9,":")+1) "  " $1 " pid " $2}' || echo "  none"
    @echo "── health:"
    @curl -sf --max-time 2 http://localhost:8100/healthz && echo || echo "  api down"

# ── quality ───────────────────────────────────────────────────────────────────

# format
fmt:
    uv run ruff format prismoid tests
    uv run ruff check --fix prismoid tests

# lint - the WHOLE service, not just prismoid/tests. `ruff check .` is what CI runs and what
# M0.1's validation replays; a lint that only looks where you remembered to point it is not a lint.
lint:
    uv run ruff check .
    uv run ruff format --check prismoid tests tools

# types (pyright strict)
types:
    uv run pyright

# import contracts - the layering law (§7)
layers:
    uv run lint-imports --config .importlinter

# the §14 greps - guardrails that a linter cannot express
audit:
    bash tools/audit.sh

# the ratchet (ADR-075): new code meets the caps; known offenders may only shrink
quality:
    uv run python tools/quality.py check

# rewrite the ratchet's baseline downward after an improvement
ratchet:
    uv run python tools/quality.py ratchet

# the before/after table a PR quotes
quality-report:
    uv run python tools/quality.py report

# the copyright header (ADR-102): name every tracked source file without COPYRIGHT.txt's notice.
# `just check` holds it through tests/laws; the Copyright workflow runs it on every pull request.
copyright:
    python3 tools/copyright_header.py

# write the copyright header into every tracked source file without it (`git add` new files first)
copyright-fix:
    python3 tools/copyright_header.py --fix

# the local reviewer (ADR-075): diff vs main, one reviewer per area, verified findings only.
# Headless and read-only; to apply findings, run `/review --fix` in an interactive session.
[working-directory: '..']
review *ARGS:
    @case " {{ARGS}} " in *" --fix "*) echo "--fix needs the interactive /review --fix" >&2; exit 1;; esac
    claude -p "/review {{ARGS}}" --allowedTools "Read" "Grep" "Glob" "Agent" "Bash(just quality-report)" "Bash(just check)" "Bash(git rev-parse:*)" "Bash(git merge-base:*)" "Bash(git diff:*)" "Bash(git show:*)" "Bash(git ls-tree:*)" "Bash(git ls-files:*)" "Bash(git cat-file:*)" "Bash(git log:*)" "Bash(git status:*)" "Bash(git blame:*)" "Bash(git grep:*)" --permission-mode dontAsk --disallowedTools "Edit" "Write" "NotebookEdit" < /dev/null

# every test - parallel across workers, each on its OWN database cloned from the dev DB
# (tests/conftest.py). The guard first: a live api/worker shares the DB with the suite, its
# minute-sweeps chew on test rows, and its connections block the template clones - two hangs
# on 2026-08-25 earned this line. `--dist loadgroup` honors the frames-a/b/c groups: frame-spinning
# files serialize onto THREE workers (a dozen parallel LibreOffice containers thrash the
# Docker VM - measured 11:29; one lane 5:48; three lanes 2:56, fully green).
test *ARGS: no-stack-guard
    uv run pytest -n auto --dist loadgroup {{ARGS}}

# unit tests only (no stack needed; fast and serial - parallel startup would cost more)
test-unit *ARGS:
    uv run pytest tests/unit tests/laws {{ARGS}}

# integration tests (needs `just up`)
test-integration *ARGS: no-stack-guard
    uv run pytest tests/integration tests/conformance -n auto --dist loadgroup {{ARGS}}

# the device's real fence and stop tests (ADR-078), by hand on a Mac: before a release, and on a new
# Chrome. CI has no Mac, so nothing else runs them. A test that would skip here (no Chrome, no Docker,
# not macOS) FAILS: green means every one ran. Needs Postgres up, `just up` or a private one named by
# PRISMOID_WORKER_DATABASE_URL: tests/conftest.py connects at import under xdist, before any test. Five
# workers, the most Chromes one Mac carries.
device-proof *ARGS: no-stack-guard
    PRISMOID_DEVICE_PROOF=1 uv run pytest tests/integration/device/test_device_chrome*.py tests/integration/device/test_device_stop*.py -n 5 --dist loadgroup {{ARGS}}

# the suite and the dev stack must never share the database (STATE 2026-08-25).
no-stack-guard:
    @! ps aux | grep -qE "[p]oem run (api|worker)" \
        || { echo "✗ a dev api/worker is running - just stop first (the suite and the stack must not share the DB)"; exit 1; }

# the gate: lint + types + layers + audit + the ratchet + unit tests + the web half (ADR-008)
check: lint types layers audit quality test-unit web-check

# the whole product in one command: submit a real goal, watch a real model work in a
# real sandbox, download what it made. Not a test suite - a demonstration.
smoke *ARGS:
    bash tools/smoke.sh {{ARGS}}

# the range drill: the trivial50 talk set as real tasks, each scored on what the person got and
# what it cost (latency, calls, Machines). Needs the dev stack + a logged-in CLI; spends cents.
range *ARGS:
    uv run python -m evals.run --suite trivial50 {{ARGS}}

# the self-hosted dev search engine (ADR-047): SearXNG with JSON enabled, on :8888.
# Then: PRISMOID_SEARCH_ENGINES=searxng PRISMOID_SEARCH_URLS="searxng=http://localhost:8888"
searxng:
    -docker rm -f searxng-dev 2>/dev/null
    docker run -d --name searxng-dev -p 8888:8080 -e SEARXNG_SECRET=dev-only-not-a-secret \
      -v {{justfile_directory()}}/deploy/searxng/settings.yml:/etc/searxng/settings.yml:ro \
      searxng/searxng

# ── drills (ADR-008: on their CI cadence or on demand - never a per-stage replay) ──

# the golden cases - real tasks, scored on the VERIFIED verdict; a dataset suite
# (--suite) takes --rows, --tag, --match, --failed-in and --sample to run only some rows (--help)
evals *ARGS:
    uv run python -m evals.run {{ARGS}}

# §15 performance budgets, warm p95 against the running API. Needs the API up + a login.
perf:
    uv run pytest tests/perf -m perf -s -p no:cacheprovider

# mixed-load test - 50 users, 70/20/10 read/read/capture. Needs the API up + a login.
load USERS="50" TIME="30s":
    uv run locust -f locustfile.py --headless -u {{USERS}} -r 10 -t {{TIME}} --host http://localhost:8111 --only-summary

# the kill matrix - a task survives a worker dying mid-flight. Needs the API up + a login.
chaos:
    bash tools/chaos.sh

# endurance - a book-length task survives a forced restart mid-run. The 12-h soak is calendar.
endurance:
    bash tools/endurance.sh

# backup/restore drill - dump the live db, restore into a fresh one, prove it is intact.
backup-restore:
    bash tools/backup_restore.sh

# freeze the frontend contract - OpenAPI + TaskEvent schema, exported from the running app.
export-contract:
    uv run python tools/export_contract.py

# ADR-054's catalog landing rule: every seed URL answers live (open) or challenges (oauth). Network required.
connectors-catalog-probe:
    uv run python tools/probe_catalog.py

# fresh-clone bring-up drill - clone -> .env -> working system, timed, target <10 min.
fresh-clone:
    bash tools/fresh_clone.sh

# ── web (the content service) ─────────────────────────────────────────────────

# the web app, dev server (Vite, http://localhost:5173)
[working-directory: '../web']
web-dev:
    pnpm dev

# build the web app (SPA - build/client is the deployable), through turbo's cache (ADR-071)
[working-directory: '..']
web-build:
    pnpm turbo run build --filter=web

# the web app for `prismoid up` (ADR-077): the API serves build/client itself, so the browser calls
# the API at its own address: `just web-local 9000` if you moved the port. Through turbo like
# web-build (ADR-071): VITE_PRISMOID_API_URL is a globalEnv, so a port is its own cache entry.
[working-directory: '..']
web-local port="8100":
    VITE_PRISMOID_API_URL=http://127.0.0.1:{{port}} pnpm turbo run build --filter=web

# regenerate the web app's API types from the frozen contract (docs/api)
[working-directory: '../web']
web-gen:
    pnpm gen

# Playwright over every surface reachable without a model run, the operator console included
# (ADR-081), on a private Postgres + Silo (S3) + worker + API and both built web apps, all removed
# when it ends. Every provider key is blanked, so no model can be called, and it is not part of
# `check`. Arguments go to `playwright test`.
# the browser flows against a private stack
[working-directory: '..']
e2e *ARGS:
    bash web/e2e/stack.sh {{ARGS}}

# The model flows (ADR-081, amended 2026-11-22): Playwright's `models` project sends real work to a
# real model on the same private stack - tasks, a follow-up, a fork, a document and its
# verification, questions, a bot's chat, evals and drafter, a routine's run. Needs a provider key
# in the environment (PRISMOID_WORKER_ANTHROPIC_API_KEY, or the OpenAI or Google one) and the
# compose frame network (`just up`), and spends about twenty cents a run; never nightly, by hand or
# from Actions -> E2E model flows.
# the browser flows that call a real model (needs a provider key)
[working-directory: '..']
e2e-models *ARGS:
    E2E_MODELS=1 bash web/e2e/stack.sh --project=models {{ARGS}}

# the web half of the gate - typecheck (typegen + tsc), oxlint, the token + radius + dash guards,
# the contrast audit, unit tests: turbo's `check` task runs them all, for web and the workspace
# packages it depends on (@prismoid/blocks) - turbo.json, ADR-071
[working-directory: '..']
web-check:
    pnpm turbo run check --filter=web...

# ── sandbar (the microVM frame daemon, ADR-013) - its own Go module in sandbar/ ──

# build both binaries (daemon + in-VM guest agent, guestd cross-built for linux)
[working-directory: '../sandbar']
sandbar-build:
    go build -o bin/sandbard ./cmd/sandbard
    GOOS=linux GOARCH=amd64 go build -o bin/sandbar-guestd ./cmd/sandbar-guestd

# sandbar's gate - vet, format check, tests (the Docker tier skips without a socket, and the live
# egress tests also need SANDBAR_RELAY_IMAGE - sandbar README, Testing)
[working-directory: '../sandbar']
sandbar-check:
    test -z "$(gofmt -l .)" || (echo 'gofmt:' && gofmt -l . && false)
    go vet ./...
    go build ./...
    GOOS=linux go build ./...
    go test ./...

# build (or reuse) the content-addressed browser image and print the two exports sandbar needs
sandbar-browser-image:
    uv run python -c "from prismoid.ai.frames.docker_driver import ensure_image; t = ensure_image('browser'); print(f'export PRISMOID_WORKER_SANDBAR_BROWSER_IMAGE={t}'); print(f'export SANDBAR_BROWSER_IMAGE={t}')"

# the sandbard image: the daemon AND `sandbard relay`, the frame network's door (ADR-063 S2)
[working-directory: '../sandbar']
sandbar-image:
    docker build -t sandbard:dev .
    @echo 'export SANDBAR_RELAY_IMAGE=sandbard:dev'

# run the daemon locally (dev; token 'dev') with egress ON behind the S2 posture: the relay on the
# default bridge, piping to this host's proxy, and sandbard joining it to the internal network it
# makes for each sandbox with domains. Names and ports read from env, so two worktrees can run their
# own daemons side by side: SANDBAR_ADDR (127.0.0.1:7070), SANDBAR_EGRESS_PORT (7080),
# SANDBAR_RELAY_NAME (sandbar-egress), SANDBAR_DOCKER_EGRESS_POOL (10.213.0.0/16; give a second
# daemon its own). The worker always sends allow_domains (ADR-021), so egress must be on. A restart
# adopts the sandboxes the last run made. For a sandbar Machine's screen, export SANDBAR_DISPLAY_KEYS
# (the public half of PRISMOID_API_SCREEN_SIGNING_KEY, sandbar/docs/api.md) before running it, and set
# PRISMOID_COMPUTER_USE_SANDBAR_SCREEN=true for the API and the worker.
[working-directory: '../sandbar']
sandbar-dev: sandbar-image
    #!/usr/bin/env bash
    set -euo pipefail
    relay="${SANDBAR_RELAY_NAME:-sandbar-egress}"
    eport="${SANDBAR_EGRESS_PORT:-7080}"
    # A fresh relay (the image may be new) has left every network; sandbard's boot joins it again.
    docker rm -f "$relay" >/dev/null 2>&1 || true
    docker run -d --name "$relay" --restart unless-stopped --user 65534:65534 --read-only --cap-drop ALL \
      --add-host host.docker.internal:host-gateway \
      -e SANDBAR_RELAY_TO="host.docker.internal:$eport" sandbard:dev relay >/dev/null
    echo "relay $relay -> host:$eport; each sandbox with domains gets its own network"
    SANDBAR_TOKEN=dev SANDBAR_BACKENDS=docker SANDBAR_BACKEND=docker SANDBAR_EGRESS=on \
      SANDBAR_ADDR="${SANDBAR_ADDR:-127.0.0.1:7070}" SANDBAR_EGRESS_ADDR="0.0.0.0:$eport" \
      SANDBAR_RELAY_CONTAINER="$relay" SANDBAR_EGRESS_URL="http://sandbar-egress:7080" \
      go run ./cmd/sandbard

# ── production deploy (deploy/DEPLOY.md) - the whole product on one Docker host ──

# build the sandbox rootfs image sandbard launches per frame (prerequisite for the stack)
[working-directory: '..']
deploy-frame-image:
    docker build -t prismoid-frame:prod backend/prismoid/ai/frames/image

# bring the full stack up (postgres · silo · sandbard · api · worker · web · caddy). Needs a
# filled deploy/.env - every ${VAR:?} in the compose file names its own generation command.
[working-directory: '..']
deploy-up: deploy-frame-image
    test -f deploy/.env || (echo 'cp deploy/.env.example deploy/.env and fill it first (PROVISION.md)' && false)
    docker compose -f deploy/compose.prod.yaml --env-file deploy/.env up --build -d
    @echo 'ingress → https://app.$DOMAIN · https://api.$DOMAIN · https://s3.$DOMAIN (deploy/Caddyfile)'

# seed the starter cloud catalog so the first task can route (§9 - edit default_models.yaml or
# `prismoid models seed --file yours.yaml` to run your own). Idempotent; needs the stack up.
[working-directory: '..']
deploy-add-models:
    docker compose -f deploy/compose.prod.yaml --env-file deploy/.env exec worker prismoid models seed

# tear the stack down (add `-v` by hand to drop the data volumes)
[working-directory: '..']
deploy-down:
    docker compose -f deploy/compose.prod.yaml --env-file deploy/.env down
