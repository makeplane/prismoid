# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A prompt grows only with evidence: the size of every prompt, and the fingerprint an eval report records.

`budget` prints each prompt's size in words against `tests/laws/prompt_budget.json`; `budget --write`
records a shrink, and refuses growth unless `--raise` says it is deliberate (the file is owner-reviewed).
The law in `tests/laws/test_law_prompt_budget.py` reads the same measurement, so this file and the test
cannot disagree. `prompts_sha` is the fingerprint an eval report carries, so a report names the prompts
it measured.

A prompt is the text the model reads: each agent's rendered instructions (identity and role, as a cloud
frame renders them) and the device law, which every agent on the user's own Mac reads on top.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path

from prismoid.ai.agents import all as agents
from prismoid.ai.agents.base import DEVICE_LAW, instructions_for

BACKEND = Path(__file__).resolve().parents[1]
BUDGET = BACKEND / "tests" / "laws" / "prompt_budget.json"

DEVICE_LAW_NAME = "device_law"


def prompt_texts() -> dict[str, str]:
    """Every prompt by name: each registered agent's rendered instructions, then the device law."""
    texts = {name: instructions_for(agents.REGISTRY[name]) for name in sorted(agents.REGISTRY)}
    return {**texts, DEVICE_LAW_NAME: DEVICE_LAW}


def word_counts() -> dict[str, int]:
    """Each prompt's size in whitespace-separated words."""
    return {name: len(text.split()) for name, text in prompt_texts().items()}


def fingerprint(texts: Mapping[str, str]) -> str:
    """One hash over every prompt in name order: it moves when any word the model reads moves."""
    whole = hashlib.sha256()
    for name in sorted(texts):
        whole.update(name.encode() + b"\0" + texts[name].encode() + b"\0")
    return whole.hexdigest()


def prompts_sha() -> str:
    """The fingerprint of this tree's prompts, as an eval report records it."""
    return fingerprint(prompt_texts())


def _dump(counts: dict[str, int]) -> str:
    return json.dumps(counts, indent=2, sort_keys=True) + "\n"


def _budget(args: argparse.Namespace) -> int:
    measured = word_counts()
    held: dict[str, int] = json.loads(BUDGET.read_text()) if BUDGET.exists() else measured
    for name in sorted(measured.keys() | held.keys()):
        print(f"{name:<16}{measured.get(name, 0):>6} words  budget {held.get(name, 0):>6}")
    if not args.write:
        return 0
    grew = {name: n for name, n in measured.items() if n > held.get(name, 0)}
    if grew and not args.raise_budget:
        print(
            f"refusing to raise {sorted(grew)}: shrink the prompt, or pass --raise with the eval delta in the PR"
        )
        return 1
    BUDGET.write_text(_dump(measured))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    budget = sub.add_parser("budget", help="print prompt sizes against the budget; --write records a shrink")
    budget.add_argument("--write", action="store_true")
    budget.add_argument("--raise", dest="raise_budget", action="store_true")
    budget.set_defaults(run=_budget)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
