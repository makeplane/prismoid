#!/usr/bin/env bash
# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# tools/audit.sh — the §14 guardrails a linter cannot express.
# Each check is a grep with a reason. A hit is a violation and exits non-zero.
# This file grows as the surfaces it guards come into existence; it never shrinks.
set -uo pipefail

SRC="prismoid"
FAIL=0

# Greps are literal-minded: they cannot tell code from the comment that explains the code.
# Every check below therefore looks for the *thing* — an import, a call, a literal — and never
# for the word. A check that fires on `# No JWT — statelessness defeats the kill switch` gets
# muted within a week, and a muted guardrail is worse than no guardrail.
G=(grep -rn --include=*.py --exclude-dir=__pycache__)

# check <name> <rule-ref> <grep args...>
check() {
  local name="$1" ref="$2" ; shift 2
  local hits
  hits="$("$@" 2>/dev/null || true)"
  if [[ -n "$hits" ]]; then
    printf '\033[31m✗ %s\033[0m  (%s)\n' "$name" "$ref"
    printf '%s\n' "$hits" | sed 's/^/    /'
    FAIL=1
  else
    printf '\033[32m✓ %s\033[0m\n' "$name"
  fi
}

[[ -d "$SRC" ]] || { echo "no $SRC yet — nothing to audit"; exit 0; }

# ── Rule 6 — the engine is persistence-free ───────────────────────────────────
check "ai/ imports neither db, operations, jobs, nor api" "Rule 6" \
  "${G[@]}" -E "^\s*(from|import)\s+prismoid\.(db|operations|jobs|api)\b" "$SRC/ai"

# ── Rule 3 — views are thin ───────────────────────────────────────────────────
check "api/ never imports ai/" "Rule 3" \
  "${G[@]}" -E "^\s*(from|import)\s+prismoid\.ai\b" "$SRC/api"

check "no raw Depends(...) in endpoint signatures — aliases only" "§14" \
  "${G[@]}" -E "Depends\(" "$SRC/api/endpoints"

check "no transactions started in api/" "Rule 3" \
  "${G[@]}" -E "session\.(commit|begin)\(" "$SRC/api"

# ── §14 — one place per kind ──────────────────────────────────────────────────
check "no select() outside db/models/" "§14" \
  "${G[@]}" -E "(^|[^_a-zA-Z.])select\(" "$SRC" --exclude-dir=models

check "no create_all — Alembic only" "Rule 11" \
  "${G[@]}" -E "create_all\(" "$SRC"

# ── §3 — deliberately-not-used dependencies (the import, never the word) ──────
check "no JWT" "§3" \
  "${G[@]}" -E "^\s*(from|import)\s+(jwt|jose|authlib)\b" "$SRC"

check "no celery / redis broker / langchain / sqlmodel" "§3" \
  "${G[@]}" -E "^\s*(from|import)\s+(celery|redis|langchain|langgraph|sqlmodel)\b" "$SRC"

# ── Rule 7 — model strings live in the provider files only ────────────────────
# The alternation is the roster of families the catalog serves; a new open-weights family
# (llama/qwen/mistral/command via the OpenAI-compatible provider) is just as much a Rule-7
# leak as a frontier one, so it belongs in the grep the moment it can be typed in code.
check "no model strings outside the provider files" "Rule 7" \
  "${G[@]}" -E "\"(claude|gpt|gemini|grok|deepseek|llama|qwen|mistral|command)-[a-z0-9.-]+\"" "$SRC" --exclude-dir=providers

# ── §14 — no URL literals outside conf/providers/links ────────────────────────
# The pattern anchors ANYWHERE inside a string literal, not just at its first character
# (widened 2026-08-24, review). `"https?://` only ever caught a URL that opened its string, so
# `_UA = "Prismoid/1.0 (+https://plane.so; …)"` walked past a guard that was supposed to
# have an opinion about it — the check did not exempt that literal, it MISSED it, and a guard
# with a blind spot is worse than no guard because it reports clean.
#
# The allowlist is now four entries, and each is a literal that is not an endpoint we CALL —
# which is the whole point of §14's URL topology: endpoints we dial must stay configurable.
#   · the Agent Plugins `$schema` identifier (ADR-045) — a spec version tag, never fetched; the
#     spec is explicit that a published identifier never reassigns to different contents, and
#     making it a setting would advertise a mutability it does not have.
#   · XML namespace URIs (`openoffice.org/2001/registry`, `w3.org/2001/XMLSchema`) — namespace
#     names, not addresses; nothing resolves them.
#   · `web_fetch`'s User-Agent (ADR-046) — how we identify OURSELVES to a server we dial, the
#     opposite of an endpoint, and the robots stance recorded in the ADR depends on it saying
#     exactly this.
#   · the dev doctor's egress probes (`runner/dev.py`) — the two URLs whose allowed/denied pair
#     IS the demonstration; configurable probes would prove nothing about the shipped allowlist.
#   · the connector catalog (`schemas/connector_catalog.py`, ADR-054) — the model-catalog
#     precedent applied to connectors: a provider's endpoint IS the data row (validated at
#     import, probed live by `just connectors-catalog-probe` before it lands), and the user's
#     own connector rows override nothing here — these are the entries the catalog OFFERS,
#     not endpoints the process dials on its own.
#   · the device helper's per-frame proxy URL (`device/egress.py`, device-machine §6.3) — its
#     own loopback port, bound at runtime, with the frame's credential, handed to the frame's
#     commands as `HTTP(S)_PROXY` (and the Prismoid Chrome's, with the `chrome` credential, and its
#     second door, ADR-078 decision 19): an address this process serves, never one it dials. The
#     seed URLs beside it name a `.invalid` host the proxy refuses: nothing ever dials them. Those
#     two are matched whole, line end included, so a real URL sharing their line is still a hit, and
#     SEED_HOST carries no scheme for this grep to see, so the check under this one holds its value.
# conf_push.py is a settings group conf.py composes, and its FCM endpoint and OAuth scope (ADR-100)
# are default settings like any in conf.py: that one group is excluded by name, never every conf_*.py.
check "no URL literals outside conf.py / providers / links.py" "§14" \
  bash -c '"$@" | grep -vE "agent-plugins\.org/schemas|openoffice\.org/2001/registry|w3\.org/2001/XMLSchema|Prismoid/[0-9.]+ \(\+https|runner/dev\.py:.*(example\.com|pypi\.org)|device/egress\.py:.*\{(frame\.token|self\.chrome_token)\}@127\.0\.0\.1:\{self\.port\}|device/egress\.py:[0-9]+: +return f\"http://127\.0\.0\.1:\{self\.socket_port\}\"$|device/egress\.py:[0-9]+:SEED_URLS: Final = \(f\"http://\{SEED_HOST\}/\", f\"https://\{SEED_HOST\}/\"\)$" || true' _ \
  "${G[@]}" -E "\"[^\"]*https?://" "$SRC" --exclude=conf.py --exclude=conf_push.py --exclude-dir=providers --exclude=links.py --exclude=connector_catalog.py

check "the proxy's seed host is the .invalid name it refuses" "§14" \
  bash -c 'grep -qxF "SEED_HOST: Final = \"proxy-seed.prismoid.invalid\"" "$1" || echo "$1: SEED_HOST changed: the audit lets its URLs pass because nothing dials a .invalid host"' _ \
  "$SRC/device/egress.py"

# ── §14 — "actor" never in external responses outside audit payloads ──────────
# The thing, not the word (again): `class ActorOut` or `ActorOut(...)` is a violation; a
# docstring explaining *why there is no ActorOut* is the opposite of one. This check fired on
# its own conscience once already — see the M0.2 log.
if [[ -d "$SRC/api/serializers" ]]; then
  check "no ActorOut defined or returned by a serializer" "§14" \
    "${G[@]}" -E "(class\s+ActorOut\b|ActorOut\(|->\s*ActorOut\b|:\s*ActorOut\b)" "$SRC/api/serializers"
fi

# ── Rule 9 — no plaintext secrets at rest ─────────────────────────────────────
check "no hardcoded provider keys" "Rule 9" \
  "${G[@]}" -E "(sk-[a-zA-Z0-9]{20,}|AKIA[0-9A-Z]{16})" "$SRC"

# ── Process — the two rulebooks never diverge ─────────────────────────────────
# Assistants read one or the other; a drift between them is two different sets of laws (an
# agent following AGENTS.md would obey rules a CLAUDE.md reader never saw). They are
# byte-identical today; this fails the moment they part. Run from backend/, so the repo-root
# rulebooks are one level up.
if [[ -f ../CLAUDE.md && -f ../AGENTS.md ]]; then
  if diff -q ../CLAUDE.md ../AGENTS.md >/dev/null 2>&1; then
    printf '\033[32m✓ AGENTS.md ≡ CLAUDE.md\033[0m\n'
  else
    printf '\033[31m✗ AGENTS.md and CLAUDE.md have diverged\033[0m  (one rulebook, two files)\n'
    FAIL=1
  fi
fi

# ── ADR-063 — only operations/machines calls lifecycle verbs on a Machine's frame ──
# A step binds, freezes, archives, suspends or destroys a frame only through `machines.*` (acquire/release,
# `discard_legacy`), so the row's (state, epoch, held_by) always knows what the frame is doing.
check "no frame lifecycle verbs in operations/execution" "ADR-063" \
  "${G[@]}" -E "\.(bind|freeze|thaw|archive|unpack|destroy|suspend|resume)\(" "$SRC/operations/execution"

# ── ADR-069 — the durable runtime has one door ────────────────────────────────
# Every job, effect, schedule, queue, park and listing goes through `jobs/durable.py`, so the engine
# swap that is planned (DBOS → DART) is that file and its tests, never every job body.
check "dbos is imported only by jobs/durable.py" "ADR-069" \
  "${G[@]}" -E "^\s*(from|import)\s+dbos\b" "$SRC" --exclude=durable.py

echo
if [[ $FAIL -eq 0 ]]; then
  printf '\033[32maudit clean\033[0m\n'
else
  printf '\033[31maudit FAILED — see the violations above (§14)\033[0m\n'
fi
exit $FAIL
