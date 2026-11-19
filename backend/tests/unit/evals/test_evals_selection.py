# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Row selection for a dataset run: `--rows`, `--tag`, `--match`, `--failed-in` and `--sample`, joined by AND.

Pure: rows and an earlier report are arguments; nothing is submitted.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from evals.metrics import SuiteKind
from evals.run import plan
from evals.runners import Trial, summary
from evals.schema import Outcome, OutcomeRow, RowGroup
from evals.selection import Sample, Selection, tags_of
from evals.trace import PrismoidTrace


def row(rid: str, message: str = "m", **fields: object) -> OutcomeRow:
    return OutcomeRow.model_validate({"id": rid, "message": message, "expected_outcome": "reply"} | fields)


ROWS = [
    row("a", "Translate this", category="translate", group="trivial"),
    row("b", "What is 2+2?", category="arithmetic", label_note="A Sum"),
    row("c", "rewrite the memo", category="rewrite", expected_outcome="plan"),
    row("d.x", "m"),
]


def ids(rows: list[OutcomeRow]) -> list[str]:
    return [r.id for r in rows]


def test_rows_keeps_only_the_named_ids_in_dataset_order() -> None:
    chosen, passed_over = Selection(rows=("c", "a")).pick(ROWS)
    assert ids(chosen) == ["a", "c"]
    assert passed_over == {"b": "not selected", "d.x": "not selected"}


def test_an_unknown_row_id_is_an_error_that_names_it() -> None:
    with pytest.raises(ValueError, match="unknown row id: nope"):
        Selection(rows=("a", "nope")).pick(ROWS)


def test_a_tag_is_any_of_group_category_variant_or_expected_outcome() -> None:
    assert tags_of(ROWS[0]) == frozenset({"translate", RowGroup.TRIVIAL.value, Outcome.REPLY.value})
    assert ids(Selection(tags=("arithmetic", "plan")).pick(ROWS)[0]) == ["b", "c"]


def test_a_tag_no_row_carries_is_an_error_that_names_it() -> None:
    with pytest.raises(ValueError, match="no row carries tag: arithmatic"):
        Selection(tags=("arithmatic",)).pick(ROWS)


def test_match_is_a_case_insensitive_plain_substring_over_id_message_and_label_note() -> None:
    assert ids(Selection(match="TRANSLATE").pick(ROWS)[0]) == ["a"]
    assert ids(Selection(match="a sum").pick(ROWS)[0]) == ["b"]  # the label note
    assert ids(Selection(match="d.x").pick(ROWS)[0]) == ["d.x"]
    assert ids(Selection(match="2+2").pick(ROWS)[0]) == ["b"]  # never read as a regex


def test_flags_combine_with_and() -> None:
    chosen, _ = Selection(tags=("reply",), match="m", rows=("a", "b", "c")).pick(ROWS)
    assert ids(chosen) == ["b"]  # c is a plan; a's id, message and note have no "m"


def test_a_selection_that_selects_nothing_is_an_error() -> None:
    with pytest.raises(ValueError, match="selects no row"):
        Selection(rows=("a",), match="zzz").pick(ROWS)


def test_no_flag_is_the_whole_suite() -> None:
    selection = Selection()
    assert not selection.partial
    assert selection.pick(ROWS) == (ROWS, {})


def earlier(trials: list[Trial]) -> summary.SuiteReport:
    return summary.report_of(
        "trivial50",
        SuiteKind.TALK,
        trials,
        k=1,
        tree="old",
        prompts_sha="p" * 64,
        date=dt.date(2026, 11, 1),
        not_run={"skipped": "a conversation row"},
    )


def test_the_failed_rows_of_a_report_are_its_failures_errors_stalls_and_timeouts() -> None:
    good = PrismoidTrace(outcome=Outcome.REPLY, delivered=True, final_text="ok", cost_usd=Decimal(0))
    wrong = PrismoidTrace(outcome=Outcome.PLAN, delivered=True, final_text="ok", cost_usd=Decimal(0))
    report = earlier(
        [
            Trial(row("passed"), 0, good),
            Trial(row("failed"), 0, wrong),
            Trial(row("timed"), 0, good, timed_out=True),
            Trial(row("errored"), 0, None, error="502"),
            Trial(row("stalled"), 0, None, error="stopped", stalled=True),
            Trial(row("flaky"), 0, good),
            Trial(row("flaky"), 1, wrong),
        ]
    )
    assert summary.failed_rows(report) == frozenset({"failed", "timed", "errored", "stalled", "flaky"})


def test_failed_in_keeps_only_the_rows_that_failed_there() -> None:
    chosen, _ = Selection(failed_in="old.json", failed=("b", "gone")).pick(ROWS)
    assert ids(chosen) == ["b"]  # a row since deleted from the dataset is not an error


def test_a_sample_is_deterministic_by_seed_keeps_dataset_order_and_draws_only_runnable_rows() -> None:
    rows = [row(f"r{n}") for n in range(20)] + [row("chat", context={"facts": {"turn": 9}})]
    draw = Sample(size=5, seed=7)
    first, skipped = plan(rows, frozenset(), None, sample=draw)
    again, _ = plan(rows, frozenset(), None, sample=draw)
    other, _ = plan(rows, frozenset(), None, sample=Sample(size=5, seed=8))
    assert ids(first) == ids(again) and len(first) == 5
    assert ids(first) != ids(other)
    assert ids(first) == sorted(ids(first), key=lambda i: int(i[1:]))
    assert skipped["chat"] != "outside --sample"  # a row the runner cannot run is never drawn
    assert sum(why == "outside --sample" for why in skipped.values()) == 15


def test_a_sample_larger_than_the_runnable_rows_takes_them_all() -> None:
    rows = [row(f"r{n}") for n in range(3)]
    assert ids(plan(rows, frozenset(), None, sample=Sample(size=10, seed=0))[0]) == ["r0", "r1", "r2"]
