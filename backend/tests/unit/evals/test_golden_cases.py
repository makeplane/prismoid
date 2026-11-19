# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The golden suite is well-formed.

Running the sixty cases takes a worker, real models, and a frontier judge. What a unit
test can guard *without* any of that is that each case is runnable and honest before a single model is
called: its source files exist, it names what it must contain, and cases 21–60 actually span the twelve
skills they claim. A broken fixture path or a dropped skill is caught here in a second, not discovered
forty minutes into a suite run.
"""

from __future__ import annotations

from pathlib import Path

from evals.cases.golden import BUILTIN_SKILLS, CASES


def test_hundred_cases_uniquely_named() -> None:
    assert len(CASES) == 104, "M2.7's twenty + M7.1's forty + M7.2's forty + ADR-015 M2's four (organize)"
    assert len({c.name for c in CASES}) == 104, "case names are the run's keys - they must be unique"


def test_every_input_fixture_exists() -> None:
    missing = sorted({p for c in CASES for p in c.inputs if not Path(p).exists()})
    assert not missing, f"golden cases reference fixtures that do not exist: {missing}"


def test_every_case_asserts_something() -> None:
    # A case with no must_contain leans entirely on the verdict; fine for the originals, but a breadth
    # case that also names a figure catches a delivered artifact that verified an *empty* answer.
    thin = [c.name for c in CASES if c.skill and not c.must_contain]
    assert not thin, f"breadth cases should anchor at least one figure/string: {thin}"


def test_breadth_spans_the_twelve_skills() -> None:
    labelled = {c.skill for c in CASES if c.skill}
    assert labelled >= BUILTIN_SKILLS, f"missing skills: {sorted(BUILTIN_SKILLS - labelled)}"
    # Each labelled skill has at least two cases - one is an anecdote, two is coverage.
    from collections import Counter

    counts = Counter(c.skill for c in CASES if c.skill)
    thin = {s: n for s, n in counts.items() if n < 2}
    assert not thin, f"skills with only one breadth case: {thin}"


def test_code_and_spec_cases_are_self_contained() -> None:
    # A coding task's brief is its prompt - attaching the financial brief would be noise the router
    # might mistake for a document task. So code/spec cases carry no source.
    for c in CASES:
        if c.skill in ("code", "spec"):
            assert c.inputs == (), f"{c.name} ({c.skill}) should have no input fixture"
