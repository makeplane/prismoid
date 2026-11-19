# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Run-over-run diff for the golden suite.

`python tools/evals_diff.py previous.json current.json` — the number is an alarm, the *diff* is a lead.
"89% vs 91%" tells you something moved; "case `revenue-split` went pass→fail" tells you where to look.
The nightly compares last night's report to tonight's and posts this, so a regression is a named case,
not a percentage a human has to bisect. Exits non-zero if any case regressed — a CI signal, not just a
report.
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any


def _cases(report: dict[str, Any]) -> dict[str, bool]:
    cases = report.get("cases", {})
    assert isinstance(cases, dict)
    return {name: bool(c.get("ok")) for name, c in cases.items()}


def diff(previous: dict[str, Any], current: dict[str, Any]) -> int:
    prev, cur = _cases(previous), _cases(current)

    prev_rate = float(previous.get("rate", 0.0))
    cur_rate = float(current.get("rate", 0.0))
    arrow = "→"
    print(f"  rate: {prev_rate:.0%} {arrow} {cur_rate:.0%}  ({cur_rate - prev_rate:+.1%})")

    regressed = sorted(n for n in cur if n in prev and prev[n] and not cur[n])
    fixed = sorted(n for n in cur if n in prev and not prev[n] and cur[n])
    added = sorted(set(cur) - set(prev))
    removed = sorted(set(prev) - set(cur))

    for name in regressed:
        print(f"  ✗ REGRESSED  {name}  (pass → fail)")
    for name in fixed:
        print(f"  ✓ fixed      {name}  (fail → pass)")
    for name in added:
        print(f"  + new        {name}  ({'pass' if cur[name] else 'fail'})")
    for name in removed:
        print(f"  - removed    {name}")

    if not (regressed or fixed or added or removed):
        print("  no change since the previous run")

    return 1 if regressed else 0


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: evals_diff.py <previous.json> <current.json>", file=sys.stderr)
        return 2
    previous_path, current_path = pathlib.Path(argv[1]), pathlib.Path(argv[2])
    if not previous_path.exists():
        print(f"  (no previous report at {previous_path} — first run, nothing to diff)")
        return 0
    previous = json.loads(previous_path.read_text())
    current = json.loads(current_path.read_text())
    return diff(previous, current)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
