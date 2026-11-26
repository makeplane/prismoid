# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The golden hundred - real tasks, scored on the **verified** verdict.

The cases are data, one JSON line each in `golden.jsonl` beside this file: `name`, `category`, `goal`,
`inputs`, `must_contain`, and optionally `skill`, `bots` and `note` (why the case is the way it is). A
field outside that set fails to load. This module loads them and holds the rules a case list must keep.

The categories:

* `single-brief` - the first twenty: document analysis over one brief.
* `breadth` - the same discipline turned on the skills (the `skill` label is what each case is written to
  exercise; routing still decides from the goal). Atlas answers most; Helios, a contrasting brief, is what
  makes a *comparison* real; code and spec cases are self-contained (no source), because a coding task's
  brief is its own prompt. What each skill's cases ask for:
  - analyze: reason over the figures, not just restate them.
  - compare: needs two things, and now there are two.
  - extract: pull structured facts out, verbatim.
  - summarize: compress without losing the load-bearing number.
  - report: a structured document for a named reader.
  - email: short, addressed, figure-anchored.
  - critique: say what is wrong or missing, not what is there.
  - model: a spreadsheet whose totals must recompute (spreadsheet suite).
  - code: a program with tests that pass (code suite). Self-contained: the spec is the prompt.
  - deck: a slide deck whose figures are grounded (deck suite).
  - spec: a technical specification (document). Self-contained.
  - research: synthesise a focused brief from the source (no egress; the brief is the corpus).
* `depth` - deeper analysis (multi-step, the answer is not a lookup), model depth (spreadsheets whose
  totals foot across more rows), code depth (programs a case can actually exercise), and depth for deck,
  email, spec, summarize, report, critique, extract and research.
* `cross-brief` - one answer from two sources.
* `sabotage` - the answer is NOT in the brief. The only correct response names what is missing; a memo that
  invents the figure and verifies its own sum is the exact failure these cases exist to catch (scored via
  the verifier, never on must_contain alone). A suite of only-answerable cases measures convergence; the
  unanswerable ones measure whether the verifier refuses to fabricate, and mixing them into one rate is
  the point of the four evaluators (a fabricated answer can be Verified-false yet WithinBudget).
* `organize` - the collection kind's lenses.
* `browser` - opt-in rows on the fixture admin app; their goal names the site as `{base}`.

**A golden case does not check that a memo was produced. It checks that a memo was produced *and
survived the verifier*.** That is the only score worth having: a suite that measures "did the model
answer" measures the model, and this system's claim is not about the model. It is about what
survives a suite, a cross-family judge, and up to three targeted retries.

So the pass condition is `verdict == pass` on a signed report. Nothing else counts, and nothing else
is asked.

**Every case is answerable from its sources.** That is not a kindness - it is the experiment's
control. A case that *cannot* be answered from its brief tests the fabrication detector (M2.1b's
validation does exactly that, deliberately); a case that *can* tests whether the whole pipeline
converges on truth. Mixing the two into one number gives a percentage that means nothing.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

ATLAS = "tests/fixtures/atlas-q3.md"
HELIOS = "tests/fixtures/helios-q3.md"  # a contrasting brief - lets a case genuinely *compare*
CASES_FILE = Path(__file__).with_name("golden.jsonl")
BASE = "{base}"  # how a browser row names the fixture site


@dataclass(frozen=True)
class Golden:
    name: str
    goal: str
    inputs: tuple[str, ...] = (ATLAS,)

    # Substrings that must survive into the delivered artifact. Deliberately sparse: the *verdict*
    # is the assertion, and a golden case that over-specifies its wording is a case that fails when
    # a model writes it better.
    must_contain: tuple[str, ...] = ()

    # The skill this case is *written to exercise* (breadth). It is a label, not a lever: routing
    # still chooses the skill from the goal text, and a case whose goal routes elsewhere is a
    # routing finding, not a rigged result. Empty on the original twenty, which predate the field.
    skill: str = ""

    # The bots to put on the task's roster - set only by the bot-golden lane, whose
    # cases exercise a specific bot the submitting credential may not otherwise carry. Unlike
    # `skill` this IS a lever (it narrows the roster), and honestly so: the case measures the
    # bot's work, and One's routing to it is measured by the selection suite, not this one.
    bots: tuple[str, ...] = ()

    category: str = ""  # a golden.jsonl case's category; empty on the skill and bot lanes' cases
    note: str = ""  # why the case is the way it is


class Category(StrEnum):
    SINGLE_BRIEF = "single-brief"
    BREADTH = "breadth"
    DEPTH = "depth"
    CROSS_BRIEF = "cross-brief"
    SABOTAGE = "sabotage"
    ORGANIZE = "organize"
    BROWSER = "browser"


class GoldenRow(BaseModel):
    """One line of `golden.jsonl`. Every field a case runs on is stated; nothing defaults to a brief."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    category: Category
    goal: str
    inputs: tuple[str, ...]
    must_contain: tuple[str, ...]
    skill: str = ""
    bots: tuple[str, ...] = ()
    note: str = ""

    def case(self, base: str) -> Golden:
        return Golden(
            name=self.name,
            goal=self.goal.replace(BASE, base),
            inputs=self.inputs,
            must_contain=self.must_contain,
            skill=self.skill,
            bots=self.bots,
            category=self.category.value,
            note=self.note,
        )


def load_cases(path: Path = CASES_FILE, base: str = "") -> tuple[Golden, ...]:
    """Every case in the file; the browser rows only when `base` names the fixture site."""
    rows: list[GoldenRow] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(GoldenRow.model_validate_json(line))
        except ValueError as error:
            raise ValueError(f"{path.name}:{number}: {error}") from error
    if unmarked := [r.name for r in rows if (BASE in r.goal) != (r.category is Category.BROWSER)]:
        raise ValueError(f"{path.name}: a browser row, and only a browser row, names {BASE}: {unmarked}")
    return tuple(r.case(base) for r in rows if r.category is not Category.BROWSER or base)


def sample(cases: tuple[Golden, ...], size: int, seed: int) -> tuple[Golden, ...]:
    """`--sample N --seed S` for the golden suite: N cases drawn by a seeded RNG, kept in file order."""
    picked = set(random.Random(seed).sample(range(len(cases)), min(size, len(cases))))
    return tuple(c for n, c in enumerate(cases) if n in picked)


# The browser golden rows are opt-in, since they need the fixture admin app reachable from a real
# task's frame (an operator runs tests/fixtures/admin/server.py on a public host and sets
# PRISMOID_EVALS_BROWSER_URL; the operator reads <base>/log after the run for the three that assert
# what happened). Absent the env var, CASES holds the hundred-and-four that
# `test_hundred_cases_uniquely_named` pins.
BROWSER_FIXTURE_URL = os.environ.get("PRISMOID_EVALS_BROWSER_URL", "").rstrip("/")
CASES = load_cases(base=BROWSER_FIXTURE_URL)

assert len({c.name for c in CASES}) == len(CASES), "golden case names must be unique"

# The breadth claim, made checkable: the labelled cases exercise every builtin skill, so a
# regression that quietly drops a skill's coverage trips here rather than passing unnoticed.
BUILTIN_SKILLS = frozenset(
    (
        "analyze",
        "compare",
        "extract",
        "summarize",
        "report",
        "email",
        "critique",
        "model",
        "code",
        "deck",
        "spec",
        "research",
        "organize",
    )
)
_labelled = frozenset(c.skill for c in CASES if c.skill)
assert _labelled >= BUILTIN_SKILLS, f"golden breadth is missing: {sorted(BUILTIN_SKILLS - _labelled)}"
