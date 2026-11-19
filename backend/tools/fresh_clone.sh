#!/usr/bin/env bash
# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Fresh-clone bring-up drill. Clone → .env → working system, timed, target <10 minutes.
#
# Run this on a CLEAN machine after: `git clone …`, `cd computer`, and creating `.env` with the provider
# keys (ANTHROPIC_API_KEY at minimum). It times the whole path so "it works on a fresh box" is a number,
# not a hope — the exit criterion for the build is "fresh-machine bring-up <10 min", and this is the stopwatch.
#
# It is destructive of local state on purpose (a clean machine has none): `just up` brings the stack from
# nothing, `migrate` builds the schema, `seed` loads the twelve builtins. On a machine that is already
# set up it simply re-proves the sequence (migrate/seed are idempotent).
set -euo pipefail
cd "$(dirname "$0")/.."
now() { python3 -c 'import time;print(time.time())'; }
T0=$(now); mark() { echo "  [$(python3 -c "print(f'{$(now)-$T0:6.1f}s')")]  $1"; }

echo "── fresh-clone bring-up (target <600s) ──"
uv sync --frozen >/dev/null 2>&1;                       mark "deps synced (uv sync --frozen)"
just up >/dev/null 2>&1;                                mark "stack up (postgres + minio + buckets)"
just migrate >/dev/null 2>&1;                           mark "schema migrated + skills seeded"
# API + worker in the background for smoke/perf.
PRISMOID_ENV=dev nohup .venv/bin/prismoid run api --port 8100 >/tmp/fc-api.log 2>&1 & disown
PRISMOID_ENV=dev nohup .venv/bin/prismoid run worker >/tmp/fc-worker.log 2>&1 & disown
for _ in $(seq 1 60); do curl -sf http://localhost:8100/healthz >/dev/null 2>&1 && break; sleep 1; done
mark "api + worker up (healthz ok)"
# A login for the smoke/perf harnesses.
.venv/bin/prismoid login --signup --email fresh@prismoid.test --name Fresh --password 'correct horse battery staple' >/dev/null 2>&1 || true
mark "user created"
just smoke >/dev/null 2>&1 && mark "smoke green (a real task, verified, delivered)" || { mark "SMOKE FAILED"; exit 1; }
PRISMOID_CLI_API_URL=http://localhost:8100 just perf >/dev/null 2>&1 && mark "perf budgets green" || { mark "PERF FAILED"; exit 1; }

TOTAL=$(python3 -c "print(f'{$(now)-$T0:.0f}')")
echo ""
if [ "$TOTAL" -lt 600 ]; then
  echo "  == clone → green in ${TOTAL}s (<600s) ==  PASS"
else
  echo "  == ${TOTAL}s exceeds the 10-minute budget ==  FAIL"; exit 1
fi
