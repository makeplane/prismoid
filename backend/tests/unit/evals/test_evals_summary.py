# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A suite run's report: the numbers it quotes, the gate it answers to, the table and the file.

Every expected value is worked out by hand in the test from the trials it builds.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from evals.metrics import SuiteKind
from evals.runners import Trial, summary
from evals.runners.summary import Gate, SuiteReport, gate_failures, gate_of, inconclusive_reasons, summarize
from evals.schema import Never, Outcome, OutcomeRow, Unscored
from evals.trace import PrismoidTrace, TokenCounts, ToolRecord

DAY = dt.date(2026, 11, 14)
REPLY_ROW = OutcomeRow(id="a", message="m", expected_outcome=Outcome.REPLY)
PLAN_ROW = OutcomeRow(id="b", message="m", expected_outcome=Outcome.PLAN)


def trace(
    outcome: Outcome | None = Outcome.REPLY,
    *,
    cost: str = "0.01",
    latency: float | None = 2.0,
    ttft: float | None = 1.0,
    calls: int = 1,
    tokens: TokenCounts = TokenCounts(),
    tools: int = 0,
    machines: int = 0,
) -> PrismoidTrace:
    return PrismoidTrace(
        outcome=outcome,
        delivered=latency is not None,
        final_text="an answer" if latency is not None else "",
        cost_usd=Decimal(cost),
        latency_s=latency,
        ttft_s=ttft,
        calls_before_delivery=calls,
        tokens=tokens,
        tool_calls=tuple(ToolRecord(tool="web.search", ok=True) for _ in range(tools)),
        machines=machines,
    )


def trial(
    row: OutcomeRow,
    index: int,
    made: PrismoidTrace | None,
    *,
    error: str = "",
    retried: bool = False,
    timed_out: bool = False,
    stalled: bool = False,
) -> Trial:
    return Trial(row, index, made, error=error, retried=retried, timed_out=timed_out, stalled=stalled)


def report(
    trials: list[Trial],
    *,
    k: int = 3,
    kind: SuiteKind = SuiteKind.TALK,
    suite: str = "s",
    tree: str = "abc",
    slack: bool = False,
    not_run: dict[str, str] | None = None,
) -> SuiteReport:
    return summary.report_of(
        suite,
        kind,
        trials,
        k=k,
        tree=tree,
        prompts_sha="p" * 64,
        date=DAY,
        not_run=not_run or {},
        slack=slack,
    )


def thrice(row: OutcomeRow, *traces: PrismoidTrace) -> list[Trial]:
    return [trial(row, i, made) for i, made in enumerate(traces)]


# ── the numbers ──────────────────────────────────────────────────────────────────────────────


def test_accuracy_is_the_mean_of_task_pass_rates_and_pass_cubed_needs_all_three() -> None:
    good = trace()
    run = report(
        thrice(REPLY_ROW, good, good, good) + thrice(PLAN_ROW, trace(Outcome.PLAN), trace(Outcome.PLAN), good)
    )
    got = summarize(run)
    assert got.accuracy is not None
    assert got.accuracy.value == pytest.approx((1.0 + 2 / 3) / 2)  # b: two of three passed
    assert got.pass_hat_3 == pytest.approx(0.5)  # a: 1.0; b: C(2,3) = 0
    assert (got.tasks, got.trials) == (2, 3)


def test_pass_cubed_is_not_quoted_for_a_run_with_fewer_than_three_trials() -> None:
    assert summarize(report(thrice(REPLY_ROW, trace()), k=1)).pass_hat_3 is None


def test_cost_latency_ttft_and_shape_are_over_every_scored_trial() -> None:
    run = report(
        thrice(
            REPLY_ROW,
            trace(cost="0.01", latency=1.0, ttft=None, calls=1, tools=0),
            trace(cost="0.03", latency=3.0, ttft=1.5, calls=2, tools=1),
            trace(cost="0.02", latency=2.0, ttft=None, calls=3, tools=2, machines=1),
        )
    )
    got = summarize(run)
    assert got.usd_per_task == pytest.approx(0.02)
    assert (got.p50_s, got.p95_s) == (2.0, 3.0)  # nearest rank over 1, 2, 3
    assert got.ttft_p50_s == 1.5  # a turn with no delta has no TTFT; it is left out, not counted as zero
    assert (got.calls_before_delivery, got.tool_calls, got.machines) == (2.0, 1.0, pytest.approx(1 / 3))


def test_the_cache_hit_rate_is_cache_reads_over_every_prompt_token() -> None:
    warm = TokenCounts(input=100, cache_read=300, cache_write=100)
    cold = TokenCounts(input=500)
    got = summarize(report(thrice(REPLY_ROW, trace(tokens=warm), trace(tokens=cold)), k=2))
    assert got.cache_hit == pytest.approx(300 / 1000)


def test_the_route_score_weighs_a_miss_by_what_it_costs() -> None:
    """A reply row answered with a plan every time costs 8 of a worst 10; a plan row met costs 0 of 4."""
    run = report(thrice(REPLY_ROW, *[trace(Outcome.PLAN)] * 3) + thrice(PLAN_ROW, *[trace(Outcome.PLAN)] * 3))
    got = summarize(run)
    assert got.route is not None
    assert got.route.value == pytest.approx(1 - 8 / 14)


def test_an_outcome_a_row_allows_is_not_a_miss() -> None:
    lenient = OutcomeRow(
        id="a", message="m", expected_outcome=Outcome.REPLY, allowed_outcomes=(Outcome.LIGHT_WORK,)
    )
    got = summarize(report(thrice(lenient, *[trace(Outcome.LIGHT_WORK)] * 3)))
    assert got.accuracy is not None and got.accuracy.value == 1.0


# ── infra errors and timeouts ────────────────────────────────────────────────────────────────


def test_an_infra_error_is_counted_and_in_no_score() -> None:
    good = thrice(REPLY_ROW, trace(), trace())
    errored = [trial(REPLY_ROW, 2, None, error="reset", retried=True)]
    got = summarize(report(good + errored))
    assert got.errors == 1
    assert got.accuracy is not None and got.accuracy.value == 1.0  # not 2/3
    assert got.usd_per_task == pytest.approx(0.01)  # an errored trial spent nothing the run can see
    assert got.inconclusive  # one of three is above 10%


def test_a_run_with_few_enough_infra_errors_is_conclusive() -> None:
    many = [trial(REPLY_ROW, i, trace()) for i in range(10)]
    got = summarize(report([*many, trial(REPLY_ROW, 10, None, error="reset")], k=11))
    assert got.errors == 1
    assert not got.inconclusive  # 1 of 11 is under 10%


def test_a_timeout_is_a_failed_trial_and_counted_beside_the_errors() -> None:
    late = trial(REPLY_ROW, 0, trace(None, latency=None), timed_out=True)
    got = summarize(report([late, trial(REPLY_ROW, 1, trace())], k=2))
    assert got.timeouts == 1
    assert (
        got.accuracy is not None and got.accuracy.value == 0.5
    )  # an agent that never answers has not answered


def test_a_timed_out_trial_never_passes_whatever_it_had_done() -> None:
    plan_row = OutcomeRow(id="p", message="m", expected_outcome=Outcome.PLAN)
    worked = PrismoidTrace(outcome=Outcome.PLAN, delivered=True)  # as if the clock ran out after delivery
    on_time, late = (summary.record_of(trial(plan_row, 0, worked, timed_out=t)) for t in (False, True))
    assert on_time is not None and on_time.passed
    assert late is not None and not late.passed


def test_a_plan_that_never_delivered_nor_asked_does_not_pass_a_plan_row() -> None:
    plan_row = OutcomeRow(id="p", message="m", expected_outcome=Outcome.PLAN)
    failed = summary.record_of(trial(plan_row, 0, PrismoidTrace(outcome=Outcome.PLAN)))
    assert failed is not None and failed.outcome is Outcome.PLAN and not failed.passed


def test_a_stalled_thread_is_named_apart_and_is_neither_infra_nor_a_score() -> None:
    stalled = trial(REPLY_ROW, 1, None, error="turn 1 of the thread did not deliver", stalled=True)
    run = report([trial(REPLY_ROW, 0, trace()), stalled], k=2)
    got = summarize(run)
    assert (got.errors, got.stalled) == (0, 1)
    assert got.inconclusive  # one trial in two never reached the row's own turn
    assert "stalled threads" in inconclusive_reasons(run)[0]
    assert run.stalled[0].error.startswith("turn 1")
    assert got.accuracy is not None and got.accuracy.value == 1.0


def test_the_checks_no_trace_can_decide_are_counted_not_passed() -> None:
    judged = OutcomeRow(id="j", message="m", expected_outcome=Outcome.REPLY, checks=(Never(value="lies"),))
    got = summarize(report(thrice(judged, trace(), trace()), k=2))
    assert got.undecided == 2


def test_a_run_where_everything_errored_has_no_scores_and_is_inconclusive() -> None:
    got = summarize(report([trial(REPLY_ROW, 0, None, error="down")], k=1))
    assert got.accuracy is None and got.route is None and got.usd_per_task is None
    assert got.inconclusive


def test_a_row_that_is_never_scored_cannot_become_a_record() -> None:
    mechanism = OutcomeRow(id="m", message="m", expected_outcome=Unscored.MECHANISM)
    with pytest.raises(ValueError, match="never run"):
        summary.record_of(trial(mechanism, 0, trace()))


# ── the gate ─────────────────────────────────────────────────────────────────────────────────


def run_of(
    outcomes: list[Outcome], *, cost: str = "0.01", latency: float = 2.0, kind: SuiteKind = SuiteKind.TALK
):
    return report(
        [
            trial(
                OutcomeRow(id=f"r{n}", message="m", expected_outcome=Outcome.REPLY),
                0,
                trace(got, cost=cost, latency=latency),
            )
            for n, got in enumerate(outcomes)
        ],
        k=1,
        kind=kind,
    )


def test_a_run_as_good_as_its_baseline_passes_the_gate() -> None:
    same = run_of([Outcome.REPLY] * 5)
    assert gate_failures(same, same) == ()


def test_a_drop_in_accuracy_that_the_paired_interval_cannot_excuse_fails_and_names_the_broken_rows() -> None:
    worse = run_of([Outcome.ASK] * 5)
    assert gate_failures(worse, run_of([Outcome.REPLY] * 5)) == (
        "regressed: accuracy",
        "regressed: broken rows (r0, r1, r2, r3, r4)",
    )


def test_a_run_that_dropped_a_row_its_baseline_scored_fails_and_names_it() -> None:
    assert gate_failures(run_of([Outcome.REPLY] * 4), run_of([Outcome.REPLY] * 5)) == (
        "regressed: missing rows (r4)",
    )


def test_one_reply_sent_to_work_fails_the_gate_whatever_the_baseline_held() -> None:
    one_plan = run_of([Outcome.PLAN, *[Outcome.REPLY] * 4])
    assert gate_failures(one_plan, one_plan) == ("replies sent to work: r0",)
    assert gate_failures(one_plan, None) == ("replies sent to work: r0",)
    assert summary.gate_of(one_plan, one_plan) is summary.Gate.FAIL


def test_a_reply_row_that_allows_a_plan_is_not_a_reply_sent_to_work() -> None:
    allowing = OutcomeRow(
        id="r", message="m", expected_outcome=Outcome.REPLY, allowed_outcomes=(Outcome.PLAN,)
    )
    run = report([trial(allowing, 0, trace(Outcome.PLAN))], k=1)
    assert gate_failures(run, run) == ()


def test_spend_more_than_five_percent_above_the_baseline_fails() -> None:
    dearer = run_of([Outcome.REPLY] * 5, cost="0.0106")
    assert gate_failures(dearer, run_of([Outcome.REPLY] * 5)) == ("regressed: cost",)


def test_talk_may_slow_by_half_a_second_and_work_by_five() -> None:
    base = run_of([Outcome.REPLY] * 5)
    assert gate_failures(run_of([Outcome.REPLY] * 5, latency=2.4), base) == ()
    assert gate_failures(run_of([Outcome.REPLY] * 5, latency=2.6), base) == ("regressed: latency",)
    work = run_of([Outcome.REPLY] * 5, kind=SuiteKind.WORK)
    assert gate_failures(run_of([Outcome.REPLY] * 5, latency=6.9, kind=SuiteKind.WORK), work) == ()


def test_a_run_drowned_in_infra_errors_is_inconclusive_however_it_scored() -> None:
    run = report([trial(REPLY_ROW, 0, trace()), trial(REPLY_ROW, 1, None, error="down")], k=2)
    assert "infra errors" in inconclusive_reasons(run)[0]
    assert gate_of(run, run) is Gate.INCONCLUSIVE


def test_the_slack_set_passes_on_three_clean_passes_of_every_row() -> None:
    clean = report(thrice(REPLY_ROW, trace(), trace(), trace()), slack=True)
    assert gate_failures(clean, None) == ()
    assert gate_of(clean, None) is Gate.PASS


def test_a_slack_row_that_was_not_run_makes_the_gate_inconclusive_never_a_pass() -> None:
    partial = report(thrice(REPLY_ROW, trace(), trace(), trace()), slack=True, not_run={"SLK-05": "no slack"})
    assert gate_failures(partial, None) == ()
    assert inconclusive_reasons(partial) == ("inconclusive: slack rows not scored: SLK-05",)
    assert gate_of(partial, None) is Gate.INCONCLUSIVE


@pytest.mark.parametrize(
    "flawed",
    [trace(tools=1), trace(machines=1), trace(calls=2)],
    ids=["a tool", "a machine", "a second call"],
)
def test_the_slack_set_fails_on_a_tool_a_machine_or_a_second_call_even_when_the_outcome_was_right(
    flawed: PrismoidTrace,
) -> None:
    run = report(thrice(REPLY_ROW, trace(), trace(), flawed), slack=True)
    assert gate_failures(run, None)


def slack_rows(count: int) -> list[OutcomeRow]:
    return [OutcomeRow(id=f"SLK-{n:02}", message="m", expected_outcome=Outcome.REPLY) for n in range(count)]


def test_a_slack_row_whose_every_trial_failed_on_infra_is_inconclusive_even_at_ten_percent() -> None:
    rows = slack_rows(10)
    clean = [t for row in rows[:9] for t in thrice(row, trace(), trace(), trace())]
    lost = [trial(rows[9], i, None, error="HTTP 502", retried=True) for i in range(3)]
    run = report([*clean, *lost], slack=True)
    assert not summarize(run).inconclusive  # 3 of 30 is not above a tenth
    assert inconclusive_reasons(run) == ("inconclusive: slack rows not scored: SLK-09",)
    assert gate_of(run, None) is Gate.INCONCLUSIVE


def test_a_slack_row_whose_thread_stalled_is_inconclusive() -> None:
    rows = slack_rows(10)
    clean = [t for row in rows[:9] for t in thrice(row, trace(), trace(), trace())]
    stalled = [
        trial(rows[9], i, None, error="turn 1 of the thread did not deliver", stalled=True) for i in range(3)
    ]
    assert gate_of(report([*clean, *stalled], slack=True), None) is Gate.INCONCLUSIVE


def test_baseline_rows_this_run_did_not_run_are_named_and_never_a_pass() -> None:
    before = run_of([Outcome.REPLY] * 5)
    fewer = report(
        [
            trial(OutcomeRow(id=f"r{n}", message="m", expected_outcome=Outcome.REPLY), 0, trace())
            for n in range(3)
        ],
        k=1,
        not_run={"r3": "connectors not connected for this user: slack", "r4": "beyond --limit"},
    )
    assert gate_failures(fewer, before) == ()  # declared, so not missing
    assert inconclusive_reasons(fewer, before) == ("inconclusive: baseline rows not run here: r3, r4",)
    assert gate_of(fewer, before) is Gate.INCONCLUSIVE


def test_the_slack_set_fails_below_pass_cubed_of_one_and_with_too_few_trials() -> None:
    flaky = report(thrice(REPLY_ROW, trace(), trace(), trace(Outcome.PLAN)), slack=True)
    assert gate_failures(flaky, None)
    assert gate_failures(report(thrice(REPLY_ROW, trace()), k=1, slack=True), None)


# ── the file ───────────────────────────────────────────────────────────────────────────────────


def test_a_report_survives_the_file_it_is_written_to(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(summary, "REPORTS", tmp_path / "reports")
    run = report(thrice(REPLY_ROW, trace(), trace(), trace(Outcome.PLAN)), suite="slack10", tree="abc123")
    path = summary.save(run)
    assert path.name == "slack10-abc123.json"
    assert summary.load(path) == run
    assert summarize(summary.load(path)) == summarize(run)
