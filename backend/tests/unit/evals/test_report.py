# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The report a suite run reads as: numbers per stratum, the confusion matrix, the rows that broke
against a baseline and the gate rule's verdict, each worked out by hand from the records built here.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from evals.metrics import Regression, SuiteKind
from evals.report import NO_ENDING, build, main, render_json, render_markdown
from evals.runners.summary import Gate, SuiteReport, TrialError, TrialRecord
from evals.schema import Outcome

DAY = dt.date(2026, 11, 14)
FAST = 400  # resamples: the intervals are not under test here, their direction and presence are


def record(
    row: str,
    expected: Outcome,
    got: Outcome | None,
    *,
    allowed: tuple[Outcome, ...] = (),
    cost: float = 0.0,
    latency: float | None = None,
) -> TrialRecord:
    return TrialRecord(
        row=row,
        dedupe_key=row,
        expected=expected,
        allowed=allowed,
        outcome=got,
        passed=got is expected or got in allowed,
        cost_usd=cost,
        latency_s=latency,
        ttft_s=None,
        calls_before_delivery=0,
        tool_calls=0,
        machines=0,
        cache_read_tokens=0,
        prompt_tokens=0,
    )


def run_of(
    records: Sequence[TrialRecord],
    *,
    suite: str = "routing",
    tree: str = "after",
    kind: SuiteKind = SuiteKind.WORK,
    not_run: dict[str, str] | None = None,
    gate: Gate = Gate.UNGATED,
    errors: tuple[TrialError, ...] = (),
) -> SuiteReport:
    return SuiteReport(
        suite=suite,
        kind=kind,
        tree=tree,
        prompts_sha="p" * 64,
        gate=gate,
        date=DAY,
        trials=1,
        records=tuple(records),
        not_run=not_run or {},
        errors=errors,
        retried=len(errors),
    )


def many(prefix: str, expected: Outcome, got: Outcome | None, count: int) -> list[TrialRecord]:
    return [record(f"{prefix}{i}", expected, got, cost=0.0) for i in range(count)]


# ── one run on its own ───────────────────────────────────────────────────────────────────────


def test_the_confusion_matrix_counts_what_each_expected_outcome_got() -> None:
    records = [
        *many("r", Outcome.REPLY, Outcome.REPLY, 3),
        record("r-bad", Outcome.REPLY, Outcome.PLAN),
        *many("p", Outcome.PLAN, Outcome.PLAN, 2),
        record("a-none", Outcome.ASK, None),
    ]
    matrix = build(run_of(records), resamples=FAST).confusion
    assert matrix.cells[Outcome.REPLY] == {Outcome.REPLY: 3, Outcome.PLAN: 1}
    assert matrix.cells[Outcome.PLAN] == {Outcome.PLAN: 2}
    assert matrix.cells[Outcome.ASK] == {NO_ENDING: 1}


def test_each_stratum_has_its_own_accuracy_with_an_interval_over_its_rows() -> None:
    records = [
        *many("r", Outcome.REPLY, Outcome.REPLY, 3),
        *many("m", Outcome.PLAN_WITH_MACHINE, Outcome.PLAN, 2),
    ]
    by_outcome = {s.expected: s for s in build(run_of(records), resamples=FAST).strata}
    reply, machine = by_outcome[Outcome.REPLY], by_outcome[Outcome.PLAN_WITH_MACHINE]
    assert (reply.rows, reply.accuracy.value) == (3, 1.0)
    assert (machine.rows, machine.accuracy.value) == (2, 0.0)
    assert machine.accuracy.low <= machine.accuracy.value <= machine.accuracy.high
    assert Outcome.PLAN not in by_outcome  # a stratum with no rows is not a line


def test_the_costliest_misroutes_come_first_and_an_allowed_outcome_is_not_one() -> None:
    records = [
        record("cheap", Outcome.REPLY, Outcome.LIGHT_WORK),  # cost 1
        record("dear", Outcome.REPLY, Outcome.PLAN_WITH_MACHINE),  # cost 10
        record("fine", Outcome.REPLY, Outcome.LIGHT_WORK, allowed=(Outcome.LIGHT_WORK,)),
        record("mid", Outcome.PLAN, Outcome.REPLY),  # cost 4
    ]
    got = build(run_of(records), resamples=FAST)
    assert [m.row for m in got.misroutes] == ["dear", "mid", "cheap"]
    assert [m.cost for m in got.misroutes] == [10, 4, 1]


def test_a_reply_sent_to_work_is_named_because_the_gate_allows_none() -> None:
    records = [
        record("sent-to-plan", Outcome.REPLY, Outcome.PLAN),
        record("sent-to-machine", Outcome.REPLY, Outcome.PLAN_WITH_MACHINE),
        record("light", Outcome.REPLY, Outcome.LIGHT_WORK),
        record("allowed", Outcome.REPLY, Outcome.PLAN, allowed=(Outcome.PLAN,)),
    ]
    assert build(run_of(records), resamples=FAST).replies_sent_to_work == ("sent-to-machine", "sent-to-plan")


def test_rows_not_run_are_counted_beside_the_score_and_never_in_it() -> None:
    records = many("r", Outcome.REPLY, Outcome.REPLY, 4)
    not_run = {"u1": "needs the transact dial", "u2": "needs the transact dial", "c1": "a conversation row"}
    got = build(run_of(records, not_run=not_run), resamples=FAST)
    assert got.summary.accuracy is not None
    assert got.summary.accuracy.value == 1.0
    assert got.not_run == {"needs the transact dial": 2, "a conversation row": 1}
    assert got.decided == 4


def test_infra_errors_are_listed_by_row_and_a_run_drowned_in_them_says_it_proves_nothing() -> None:
    errors = tuple(TrialError(row=f"e{i}", error="POST /v1/tasks: HTTP 502") for i in range(2))
    text = render_markdown(
        build(run_of(many("r", Outcome.REPLY, Outcome.REPLY, 3), errors=errors), resamples=FAST)
    )
    assert "- `e0`: POST /v1/tasks: HTTP 502" in text
    assert "2 trial(s) retried" in text
    lines = text.splitlines()
    accuracy = next(i for i, line in enumerate(lines) if line.startswith("| accuracy |"))
    assert lines[accuracy + 1] == "| not scored: infra/stalled | 2/0 |"  # beside accuracy, never hidden
    assert text.index("INCONCLUSIVE") < text.index("Numbers")


def test_a_live_run_quotes_its_cost_and_latency() -> None:
    live = run_of(
        [record("r", Outcome.REPLY, Outcome.REPLY, cost=0.02, latency=3.0)],
        suite="trivial50",
        kind=SuiteKind.TALK,
    )
    text = render_markdown(build(live, resamples=FAST))
    assert "INCONCLUSIVE" not in text
    assert "$0.0200" in text
    assert "3.0s" in text


# ── against a baseline ───────────────────────────────────────────────────────────────────────


def baseline_and_after(*, broken: int, fixed: int, steady: int) -> tuple[SuiteReport, SuiteReport]:
    """`steady` rows pass in both runs; `broken` pass before and miss after; `fixed` the reverse."""
    before = [
        *many("steady", Outcome.REPLY, Outcome.REPLY, steady),
        *many("broken", Outcome.REPLY, Outcome.REPLY, broken),
        *many("fixed", Outcome.REPLY, Outcome.PLAN, fixed),
    ]
    after = [
        *many("steady", Outcome.REPLY, Outcome.REPLY, steady),
        *many("broken", Outcome.REPLY, Outcome.PLAN, broken),
        *many("fixed", Outcome.REPLY, Outcome.REPLY, fixed),
    ]
    return run_of(before, tree="before"), run_of(after, tree="after")


def test_the_rows_that_broke_and_the_rows_that_got_fixed_are_named_with_what_they_got() -> None:
    before, after = baseline_and_after(broken=2, fixed=1, steady=3)
    against = build(after, before, resamples=FAST).against
    assert against is not None
    assert [(c.row, c.before, c.after) for c in against.broken] == [
        ("broken0", Outcome.REPLY, Outcome.PLAN),
        ("broken1", Outcome.REPLY, Outcome.PLAN),
    ]
    assert [c.row for c in against.fixed] == ["fixed0"]


def test_the_paired_delta_is_over_the_rows_both_runs_hold_and_its_interval_shows_a_real_drop() -> None:
    before, after = baseline_and_after(broken=6, fixed=0, steady=2)
    against = build(after, before, resamples=FAST).against
    assert against is not None
    assert against.accuracy.value == pytest.approx(-6 / 8)
    assert against.accuracy.high < 0  # the whole interval is below zero: a drop, not noise
    assert Regression.ACCURACY in against.regressions


def test_a_small_wobble_inside_the_interval_is_not_a_regression_but_a_cost_rise_is() -> None:
    before, after = baseline_and_after(broken=0, fixed=0, steady=8)
    dearer = after.model_copy(
        update={"records": tuple(r.model_copy(update={"cost_usd": 0.10}) for r in after.records)}
    )
    cheaper = before.model_copy(
        update={"records": tuple(r.model_copy(update={"cost_usd": 0.05}) for r in before.records)}
    )
    against = build(dearer, cheaper, resamples=FAST).against
    assert against is not None
    assert against.regressions == (Regression.COST,)
    steady = build(after, before, resamples=FAST).against
    assert steady is not None
    assert steady.regressions == ()


def test_each_stratum_carries_its_own_paired_delta() -> None:
    before = run_of([*many("r", Outcome.REPLY, Outcome.REPLY, 4), *many("p", Outcome.PLAN, Outcome.PLAN, 4)])
    after = run_of(
        [*many("r", Outcome.REPLY, Outcome.PLAN, 4), *many("p", Outcome.PLAN, Outcome.PLAN, 4)], tree="after"
    )
    deltas = {s.expected: s.delta.value for s in build(after, before, resamples=FAST).strata if s.delta}
    assert deltas == {Outcome.REPLY: -1.0, Outcome.PLAN: 0.0}


def test_rows_only_one_run_decided_are_counted_not_compared() -> None:
    before = run_of(many("a", Outcome.REPLY, Outcome.REPLY, 3))
    after = run_of(
        [*many("a", Outcome.REPLY, Outcome.REPLY, 2), *many("new", Outcome.REPLY, Outcome.REPLY, 2)]
    )
    against = build(after, before, resamples=FAST).against
    assert against is not None
    assert (against.shared, against.only_after, against.only_before) == (2, 2, 1)


def test_two_runs_that_share_no_row_cannot_be_compared_and_say_so() -> None:
    before = run_of(many("a", Outcome.REPLY, Outcome.REPLY, 2))
    after = run_of(many("b", Outcome.REPLY, Outcome.REPLY, 2))
    with pytest.raises(ValueError, match="share no row"):
        build(after, before, resamples=FAST)


def test_a_run_is_never_compared_with_another_suites() -> None:
    other = run_of(many("a", Outcome.REPLY, Outcome.REPLY, 2), suite="trivial50")
    with pytest.raises(ValueError, match=r"'routing'.*'trivial50'"):
        build(run_of(many("a", Outcome.REPLY, Outcome.REPLY, 2)), other, resamples=FAST)


def test_the_markdown_leads_with_what_regressed() -> None:
    before, after = baseline_and_after(broken=6, fixed=0, steady=2)
    text = render_markdown(build(after, before, resamples=FAST))
    assert text.index("Regressed") < text.index("Confusion")
    assert "accuracy" in text.split("Regressed")[1].splitlines()[0]
    assert "broken0" in text
    assert "before" in text and "after" in text  # both trees are named


def test_a_run_with_nothing_regressed_says_so_in_words() -> None:
    before, after = baseline_and_after(broken=0, fixed=0, steady=8)
    assert "Regressed: none" in render_markdown(build(after, before, resamples=FAST))


def test_the_json_is_the_same_report_in_machine_form() -> None:
    before, after = baseline_and_after(broken=6, fixed=0, steady=2)
    data = json.loads(render_json(build(after, before, resamples=FAST)))
    assert data["suite"] == "routing"
    assert data["against"]["regressions"] == ["accuracy", "broken rows"]
    assert data["confusion"]["cells"]["reply"]["plan"] == 6
    assert data["strata"][0]["expected"] == "reply"


def test_the_verdict_leads_and_says_why_a_run_did_not_pass() -> None:
    sent = run_of([record("r0", Outcome.REPLY, Outcome.PLAN), record("r1", Outcome.REPLY, Outcome.REPLY)])
    got = build(sent, resamples=FAST)
    assert got.gate is Gate.FAIL
    text = render_markdown(got)
    assert text.splitlines()[2] == "Gate: fail (replies sent to work: r0)"


def saved(tmp_path: Path, name: str, report: SuiteReport) -> str:
    path = tmp_path / f"{name}.json"
    path.write_text(report.model_dump_json())
    return str(path)


def test_gate_exits_zero_only_on_a_pass_two_with_no_rule_and_one_otherwise(tmp_path: Path) -> None:
    good = saved(tmp_path, "good", run_of(many("a", Outcome.REPLY, Outcome.REPLY, 2)))
    sent = saved(tmp_path, "sent", run_of(many("a", Outcome.REPLY, Outcome.PLAN, 2)))
    assert main([good, "--gate"]) == 2  # no baseline and no suite rule: nothing to hold it to
    assert main([good, "--baseline", good, "--gate"]) == 0
    assert main([sent, "--gate"]) == 1  # a reply sent to work fails with or without a baseline
    assert main([sent, "--baseline", good, "--gate"]) == 1
    assert main([sent]) == 0  # without --gate it only prints
