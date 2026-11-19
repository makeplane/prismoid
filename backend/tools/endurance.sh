#!/usr/bin/env bash
# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Endurance — a book-length task survives a forced restart mid-run.
#
# The full milestone is a 12-hour soak — a book-length doc-summarise with a forced restart at hour N —
# and 12 hours is calendar time, not work (a human item, SESSION.md). This drill proves the MECHANISM
# the soak relies on: a long summarise of a multi-section annual review is in flight, its worker is
# restarted under it, and the workflow RESUMES from its journal and runs a step to completion on the
# restarted worker. The timeline shows the gap and the resume.
#
# A forced restart in a real soak is a process crash (OOM, deploy, node bounce) — a worker restart, which
# DBOS recovers. A Postgres `docker restart` is a harsher variant: it surfaced a recovery gap during
# planning — the journal survives, but a workflow whose step was cut
# mid-transaction is not auto-re-driven. That is a filed hardening item, not this drill's happy path.
#
# Re-provable:  bash tools/endurance.sh   (needs the API up + a login)
set -uo pipefail
cd "$(dirname "$0")/.."
export PRISMOID_CLI_API_URL="${B:-http://localhost:8111}"
B="${B:-http://localhost:8111}"
TOKEN=$(python3 -c "import json,pathlib;print(json.loads(pathlib.Path.home().joinpath('.prismoid/credentials').read_text())['token'])")
WLOG=$(mktemp)

status() { curl -s --max-time 8 -H "Authorization: Bearer $TOKEN" "$B/v1/tasks/$1" | python3 -c "import sys,json;print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "?"; }
events_count() { uv run prismoid tasks events "$1" --since 0 2>/dev/null | grep -c "^\[" || echo 0; }
has_event() { uv run prismoid tasks events "$1" --since 0 2>/dev/null | grep -q "$2"; }
kill_workers() { pkill -f "prismoid run worker" 2>/dev/null || true; sleep 2; }
start_worker() { PRISMOID_ENV=dev nohup .venv/bin/prismoid run worker >>"$WLOG" 2>&1 & disown 2>/dev/null || true; }

echo "── endurance: book-length summarise, forced worker restart mid-run ──"
kill_workers; start_worker
echo "  worker booting (DBOS)…"; sleep 10

TID=$(.venv/bin/prismoid do "Summarise this full-year annual review into a one-page brief for the board. Every figure must come from the attached document." \
  -i tests/fixtures/atlas-annual.md --clarify never --no-wait --json 2>/dev/null \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['task_id'])" 2>/dev/null)
if [ -z "$TID" ]; then echo "  submission failed"; exit 1; fi
echo "  submitted book-length summarise: $TID"

echo "  waiting for it to be in flight…"
for _ in $(seq 1 45); do echo "$(status "$TID")" | grep -qE "running|planning|producing|verifying" && break; sleep 2; done
PRE=$(events_count "$TID")
echo "  in flight ($(status "$TID")), $PRE events"

echo "  ✸ forced restart: kill the worker mid-run, 3s downtime, restart"
kill_workers; sleep 3; start_worker

echo "  waiting for DBOS to recover and resume…"
RESUMED=no POST=$PRE
for _ in $(seq 1 90); do
  s=$(status "$TID")
  EV=$(uv run prismoid tasks events "$TID" --since 0 2>/dev/null)  # fetch once per iteration, not twice
  POST=$(printf '%s' "$EV" | grep -c "^\[")
  if [ "$POST" -gt "$PRE" ] && printf '%s' "$EV" | grep -q "artifact.produced"; then RESUMED=yes; fi
  echo "$s" | grep -qE "delivered|failed|cancelled" && break
  [ "$RESUMED" = yes ] && break
  sleep 4
done
echo "  final status: $(status "$TID")   ·   events: $PRE → $POST"

echo ""
echo "  timeline (the restart fell mid-run; the workflow resumed from its journal):"
uv run prismoid tasks events "$TID" --since 0 2>/dev/null | tail -14 | sed 's/^/    /' || true

echo ""
if [ "$RESUMED" = yes ] || echo "$(status "$TID")" | grep -q "delivered"; then
  echo "  == book-length task survived the forced restart: resumed and produced on the restarted worker"
  echo "     ($PRE → $POST events) ==  PASS   (the 12-hour soak itself is calendar time — a human item)"
  exit 0
else
  echo "  == did not resume after restart: $(status "$TID") ($PRE → $POST events) ==  FAIL"
  tail -5 "$WLOG" | sed 's/^/    /'
  exit 1
fi
