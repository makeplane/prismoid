# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A prompt grows only with evidence (the measurement program; `ai/agents/CLAUDE.md`: prompts carry rules
and every word is paid for).

Population: every prompt the model reads, found in code by `tools/prompt_evidence.prompt_texts`: each
registered agent's rendered instructions (identity and role) and the device law. `prompt_budget.json`
holds each one's size in words, taken from today's tree. A prompt over its budget fails; a prompt with no
entry fails; and the budget must equal the tree, so a shrink is recorded the day it lands and the number
can only go down. Raising it is an owner-reviewed edit that comes with a green eval report for the
change, never a way past the failure.

Fix a failure by cutting words: state the general rule once, or drop a case line.
`uv run python tools/prompt_evidence.py budget --write` records a shrink.
"""

from __future__ import annotations

import json
from pathlib import Path

from tools import prompt_evidence

BUDGET = Path(prompt_evidence.BUDGET)

HOW_TO_FIX = (
    "Cut words instead: state the general rule once, drop the case line. A raise is the owner's edit of "
    "prompt_budget.json, with a green eval report for it."
)


def budget_problems(measured: dict[str, int], budget: dict[str, int]) -> list[str]:
    """Where `measured` is over its budget or has none; empty means the law holds."""
    problems: list[str] = []
    for name, words in sorted(measured.items()):
        if name not in budget:
            problems.append(f"{name} has no budget ({words} words). {HOW_TO_FIX}")
        elif words > budget[name]:
            problems.append(f"{name} is {words} words, budget {budget[name]}. {HOW_TO_FIX}")
    return problems


def test_every_prompt_is_within_its_budget() -> None:
    problems = budget_problems(prompt_evidence.word_counts(), json.loads(BUDGET.read_text()))
    assert not problems, "\n".join(problems)


def test_the_budget_is_tight() -> None:
    """A shrink must be recorded, or the saved words could creep back unseen."""
    assert json.loads(BUDGET.read_text()) == prompt_evidence.word_counts(), (
        "prompt_budget.json is not today's prompt sizes: record a shrink with "
        "`uv run python tools/prompt_evidence.py budget --write`, and drop the line of a prompt that is gone."
    )


def test_the_law_bites() -> None:
    assert budget_problems({"writer": 101}, {"writer": 100}) == [
        f"writer is 101 words, budget 100. {HOW_TO_FIX}"
    ]
    assert budget_problems({"lead": 10}, {"writer": 100}) == [f"lead has no budget (10 words). {HOW_TO_FIX}"]
    assert budget_problems({"writer": 100}, {"writer": 100, "gone": 5}) == []


def test_the_fingerprint_moves_with_a_word_and_a_name() -> None:
    base = {"writer": "write well", "device_law": "stay in /work"}
    assert prompt_evidence.fingerprint(base) == prompt_evidence.fingerprint(dict(reversed(base.items())))
    assert prompt_evidence.fingerprint(base) != prompt_evidence.fingerprint({**base, "writer": "write badly"})
    assert prompt_evidence.fingerprint(base) != prompt_evidence.fingerprint(
        {"lead": "write well", "device_law": "stay in /work"}
    )
    assert set(prompt_evidence.prompt_texts()) >= {"device_law", "writer"}
