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

import json
from pathlib import Path

import pytest

from evals.cases.golden import BUILTIN_SKILLS, CASES, CASES_FILE, Category, load_cases, sample


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


def _write(tmp_path: Path, *rows: dict[str, object]) -> Path:
    path = tmp_path / "golden.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


ROW: dict[str, object] = {
    "name": "memo",
    "category": "single-brief",
    "goal": "Write the memo.",
    "inputs": [],
    "must_contain": ["41.6"],
}
BROWSER_ROW: dict[str, object] = {
    **ROW,
    "name": "site",
    "category": "browser",
    "goal": "Open {base}/index.html",
}


def test_the_cases_are_data_in_their_jsonl_file() -> None:
    assert load_cases(CASES_FILE) == CASES
    assert {c.category for c in CASES} <= set(Category)


def test_a_field_outside_the_schema_fails_to_load(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"golden\.jsonl:1"):
        load_cases(_write(tmp_path, {**ROW, "musts": ["x"]}))


def test_a_case_states_its_inputs_rather_than_defaulting_to_a_brief(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="inputs"):
        load_cases(_write(tmp_path, {k: v for k, v in ROW.items() if k != "inputs"}))


def test_a_browser_row_comes_only_with_a_site_and_names_it(tmp_path: Path) -> None:
    path = _write(tmp_path, ROW, BROWSER_ROW)
    assert [c.name for c in load_cases(path)] == ["memo"]
    with_site = load_cases(path, base="https://fixture.invalid")
    assert [c.goal for c in with_site] == ["Write the memo.", "Open https://fixture.invalid/index.html"]


def test_only_a_browser_row_may_name_the_site(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="only a browser row"):
        load_cases(_write(tmp_path, {**ROW, "goal": "Open {base}/x"}))
    with pytest.raises(ValueError, match="only a browser row"):
        load_cases(_write(tmp_path, {**BROWSER_ROW, "goal": "Open the site"}))


def test_a_sample_is_seeded_and_kept_in_file_order() -> None:
    drawn = sample(CASES, 20, seed=1)
    assert drawn == sample(CASES, 20, seed=1)
    assert len(drawn) == 20 and drawn != sample(CASES, 20, seed=2)
    order = [c.name for c in CASES]
    assert [c.name for c in drawn] == sorted((c.name for c in drawn), key=order.index)
    assert sample(CASES, 500, seed=1) == CASES
