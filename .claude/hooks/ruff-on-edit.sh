#!/bin/sh
# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# PostToolUse hook (ADR-075): format and lint a Python file the agent just edited.
# Silent no-op without jq or uv; exit 2 hands remaining lint errors back to Claude.
command -v jq >/dev/null 2>&1 || exit 0
command -v uv >/dev/null 2>&1 || exit 0
root="${CLAUDE_PROJECT_DIR:-}"
[ -n "$root" ] || exit 0
file=$(jq -r '.tool_input.file_path // empty' 2>/dev/null)
case "$file" in
  "$root"/backend/*.py) ;;
  *) exit 0 ;;
esac
[ -f "$file" ] || exit 0
uv run --directory "$root/backend" ruff format -q "$file" >/dev/null 2>&1
out=$(uv run --directory "$root/backend" ruff check --fix --unfixable F401,F841 -q "$file" 2>&1) || {
  printf '%s\n' "$out" >&2
  exit 2
}
exit 0
