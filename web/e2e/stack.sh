#!/usr/bin/env bash
# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# `just e2e`: the smoke flows against a private stack (ADR-081). It starts its own Postgres and
# MinIO containers, migrates and seeds, starts the worker and then the API (the API needs the
# durable system database the worker creates), runs Playwright (which builds and serves the web
# app itself, playwright.config.ts) and tears everything down again, pass or fail.
#
# Its ports sit away from the dev stack's (5433, 9000, 8100, 5173), so both can run at once; move
# them with E2E_PG_PORT, E2E_MINIO_PORT, E2E_API_PORT and E2E_WEB_PORT. E2E_STACK=external skips
# the boot and runs the flows against the API at E2E_API_URL. Arguments go to `playwright test`.
# No provider key is needed: no flow asks a model for anything.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
NAME="${E2E_NAME:-prismoid-e2e}"
PG_PORT="${E2E_PG_PORT:-55574}"
MINIO_PORT="${E2E_MINIO_PORT:-55584}"
API_PORT="${E2E_API_PORT:-8304}"
WEB_PORT="${E2E_WEB_PORT:-5304}"
SILO_IMAGE="pgsty/silo:RELEASE.2026-10-26T00-00-00Z@sha256:7bb2aff6b89efe1cb1303c10fc21d579447434ed06649ac9ec05a5e2dbf30ad7"
LOGS="${E2E_LOGS:-$ROOT/web/e2e/.state/logs}"
export E2E_API_URL="${E2E_API_URL:-http://127.0.0.1:$API_PORT}"
export E2E_WEB_PORT="$WEB_PORT"

run_flows() {
  cd "$ROOT/web"
  pnpm exec playwright install --only-shell chromium >/dev/null
  pnpm exec playwright test "$@"
}

if [ "${E2E_STACK:-}" = "external" ]; then
  run_flows "$@"
  exit
fi

PIDS=()
cleanup() {
  local status=$?
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do kill "$pid" 2>/dev/null || true; done
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do
    for _ in $(seq 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    kill -9 "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
  docker rm -f "$NAME-pg" "$NAME-minio" >/dev/null 2>&1 || true
  [ "$status" -eq 0 ] || echo "e2e: failed - the API and worker logs are in $LOGS" >&2
  exit "$status"
}
trap cleanup EXIT

# Wait up to $1 seconds for the command after it to succeed.
wait_for() {
  local seconds=$1
  shift
  for _ in $(seq "$seconds"); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
  echo "e2e: timed out waiting for: $*" >&2
  return 1
}

mkdir -p "$LOGS"
docker rm -f "$NAME-pg" "$NAME-minio" >/dev/null 2>&1 || true
docker run -d --name "$NAME-pg" -p "127.0.0.1:$PG_PORT:5432" \
  -e POSTGRES_USER=prismoid -e POSTGRES_PASSWORD=prismoid -e POSTGRES_DB=prismoid \
  postgres:17-alpine postgres -c max_connections=200 >/dev/null
# Silo, the maintained fork of the MinIO server (the same pin as deploy/compose.yaml); it makes no
# buckets by itself, so the `mcli` client it carries makes them once the server answers.
docker run -d --name "$NAME-minio" -p "127.0.0.1:$MINIO_PORT:9000" \
  -e MINIO_ROOT_USER=prismoid -e MINIO_ROOT_PASSWORD=prismoid1234 \
  "$SILO_IMAGE" server /data >/dev/null
wait_for 60 docker exec "$NAME-pg" pg_isready -U prismoid -d prismoid
wait_for 60 docker exec "$NAME-minio" mcli ready local
docker exec "$NAME-minio" sh -c 'mcli alias set e2e http://127.0.0.1:9000 prismoid prismoid1234 &&
  for b in artifacts snapshots skills; do mcli mb --ignore-existing "e2e/$b"; done' >/dev/null

# A person's own ~/.prismoid/config never reaches this stack, and its machines carry their own label.
export PRISMOID_CONFIG_FILE=/nonexistent/prismoid-config
export PRISMOID_ENV=dev
export PRISMOID_WORKER_INSTANCE="$NAME"
export PRISMOID_WORKER_DATABASE_URL="postgresql://prismoid:prismoid@127.0.0.1:$PG_PORT/prismoid"
export PRISMOID_API_DATABASE_URL="postgresql+asyncpg://prismoid:prismoid@127.0.0.1:$PG_PORT/prismoid"
export PRISMOID_WORKER_S3_ENDPOINT="http://127.0.0.1:$MINIO_PORT"
export PRISMOID_S3_PUBLIC_ENDPOINT="http://127.0.0.1:$MINIO_PORT"
export PRISMOID_API_PUBLIC_BASE_URL="$E2E_API_URL"
export PRISMOID_API_CORS_ORIGINS="http://localhost:$WEB_PORT,http://127.0.0.1:$WEB_PORT"
export PRISMOID_API_WEB_BASE_URL="http://localhost:$WEB_PORT"

cd "$ROOT/backend"
uv run alembic upgrade head >"$LOGS/migrate.log" 2>&1
uv run prismoid models seed >>"$LOGS/migrate.log" 2>&1
uv run prismoid skills seed >>"$LOGS/migrate.log" 2>&1

# The venv's own entry point, not `uv run`, so the pid kept is the process itself.
.venv/bin/prismoid run worker >"$LOGS/worker.log" 2>&1 &
PIDS+=($!)
wait_for 180 grep -q "worker ready" "$LOGS/worker.log"
.venv/bin/prismoid run api --port "$API_PORT" >"$LOGS/api.log" 2>&1 &
PIDS+=($!)
wait_for 60 curl -fsS "$E2E_API_URL/healthz"
echo "e2e: stack up - API $E2E_API_URL, web http://localhost:$WEB_PORT"

run_flows "$@"
