#!/usr/bin/env bash
# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# `just smoke` — the whole product, in one command.
#
# Not a test suite. A **demonstration**: submit a real goal, watch a real model work inside a real
# sandbox, and download the thing it made. If this is green, the spine is alive.
set -euo pipefail
export B="${B:-http://localhost:8100}"
export PRISMOID_CLI_API_URL="$B"

ASSERT_VERIFIED=0
[[ "${1:-}" == "--assert-verified" ]] && ASSERT_VERIFIED=1

fail() { printf '\033[31m✗ %s\033[0m\n' "$1"; exit 1; }
ok()   { printf '\033[32m✓ %s\033[0m\n' "$1"; }

curl -sf "$B/healthz" >/dev/null || fail "the API is not up (just api)"
ok "api healthy"

T=$(uv run python -c "import sys; from prismoid.runner.client import token_for; t = token_for(sys.argv[1]); print(t) if t else sys.exit(1)" "$B" 2>/dev/null) \
  || fail "not logged in (prismoid login)"
ok "authenticated"

[[ $(uv run prismoid models list --json | jq '[.[] | select(.enabled)] | length') -gt 0 ]] \
  || fail "no enabled model (prismoid models add …)"
ok "a model is routable"

# **A sourced task, because verification of an unsourced opinion is not verification.**
#
# This was "write a short memo on why verification beats vibes" — with nothing attached. It produced
# a perfectly good memo and a verdict of `unverified`, which is the suite being *correct*: there is
# nothing to ground a claim against, so no substantive check can pass, and `verdict_of` refuses to
# call that a PASS (M2.4 — `bytes:sha256` succeeds on every fabrication ever written).
#
# `--assert-verified` therefore has to ask for something a verifier can actually check. This is the
# product's real shape: here is a source, tell me what it says, and prove it.
uv run prismoid do "Using the attached brief, state Atlas Robotics' Q3 revenue and its sequential growth over Q2. Every figure must come from the brief." \
  --input tests/fixtures/atlas-q3.md --no-wait >/dev/null
ID=$(uv run prismoid tasks list --json | jq -r '.[0].id')
ok "task submitted: $ID"

# 10 minutes. A local 3B model sharing 7.6 GiB with Postgres, MinIO, Ollama and a sandbox takes
# its time — 120 s on a good run, several minutes under memory pressure. A frontier model does
# this in seconds; the timeout is generous because the *machine* is the constraint, not the code.
for i in $(seq 1 120); do
  S=$(uv run prismoid tasks show "$ID" --json 2>/dev/null | jq -r .status)
  [[ "$S" == "delivered" ]] && { ok "delivered in $((i*5))s"; break; }
  [[ "$S" == "failed" ]] && fail "task failed — prismoid tasks events $ID"
  sleep 5
  [[ $i == 120 ]] && fail "timed out after 10 minutes"
done

# The first timing assertion the smoke ever had (ADR-049 W0 — the audit found every §15
# temporal budget asserted nowhere). From the RECORD, not the poll loop's 5 s granularity:
# created→dequeued (queue hop 1) and created→delivered, with a generous end-to-end ceiling —
# the tight per-lane gates live in `just range`; this one catches "minutes" regressions.
curl -N -s -H "Authorization: Bearer $T" "$B/v1/tasks/$ID/events?since=0&follow=false" | python3 -c "
import sys, json
from datetime import datetime
ev = {}
for line in sys.stdin:
    if line.startswith('data: '):
        d = json.loads(line.removeprefix('data: '))
        ev.setdefault(d.get('type'), d)
def ts(k):
    e = ev.get(k)
    return datetime.fromisoformat(e['at'].replace('Z','+00:00')) if e else None
t0, dq, dv = ts('task.created'), ts('task.dequeued'), ts('task.delivered')
if t0 and dq:
    print(f'  hop 1 (created→dequeued): {(dq-t0).total_seconds():.2f}s')
if t0 and dv:
    total = (dv-t0).total_seconds()
    print(f'  created→delivered: {total:.1f}s')
    assert total < 600, f'delivered took {total:.0f}s — past the smoke ceiling'
" || fail "timing assertion failed"
ok "timing: created→delivered inside the smoke ceiling (hop ledger printed above)"

AID=$(curl -N -s -H "Authorization: Bearer $T" "$B/v1/tasks/$ID/events?since=0&follow=false" | python3 -c "
import sys, json
for line in sys.stdin:
    if line.startswith('data: '):
        d = json.loads(line.removeprefix('data: '))
        if d.get('type') == 'artifact.produced': print(d['artifact_id'])")
[[ -n "$AID" ]] || fail "nothing was published — the contract declared an output"
ok "artifact produced: $AID"

uv run prismoid artifacts download "$AID" -o /tmp/smoke-artifact >/dev/null || fail "download failed"
ok "downloaded, sha256 verified"

MODEL=$(uv run prismoid artifacts show "$AID" --json | jq -r .provenance.model)
[[ -n "$MODEL" && "$MODEL" != "null" && "$MODEL" != "" ]] || fail "no provenance.model"
ok "provenance: made by $MODEL"

COST=$(uv run prismoid tasks usage "$ID" --json | jq -r .total_usd)
CALLS=$(uv run prismoid tasks usage "$ID" --json | jq -r .calls)
[[ "$CALLS" -gt 0 ]] || fail "the model was called but nothing reached the ledger (Rule 8)"
ok "ledger: $CALLS call(s), \$$COST"

if [[ $ASSERT_VERIFIED == 1 ]]; then
  V=$(uv run prismoid artifacts show "$AID" --json | jq -r .verdict)
  [[ "$V" == "pass" ]] || fail "not verified (verdict=$V) — verification lands at M2"
  ok "verified"
fi

printf '\n\033[32msmoke green\033[0m\n'
