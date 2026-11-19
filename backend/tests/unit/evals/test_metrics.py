# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The metrics, against values worked out by hand.

Every expected number below is arithmetic done on paper first; the intervals are checked by what must
hold of any percentile bootstrap (a constant has no spread, the same seed gives the same interval,
a bound never leaves the data) because their exact edges depend on the resampling.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import count

import pytest

from evals.metrics import (
    MISROUTE_COST,
    Regression,
    RoutedTask,
    SuiteKind,
    SuiteRun,
    Trials,
    accuracy,
    bootstrap_interval,
    nearest_rank,
    paired_delta,
    pass_hat_k,
    regressions,
    route_score,
    task_means,
)
from evals.schema import Outcome, Unscored

FAST = {"resamples": 500}  # the exact edges are not under test, so keep the resampling short


def trials(**tasks: str) -> Trials:
    """`a="TTF"` is task a with three trials: pass, pass, fail."""
    return {name: [mark == "T" for mark in marks] for name, marks in tasks.items()}


# ── accuracy and reliability ─────────────────────────────────────────────────────────────────


def test_accuracy_is_the_mean_over_tasks_of_the_mean_over_trials() -> None:
    results = trials(a="TF", b="TT")
    assert task_means(results) == {"a": 0.5, "b": 1.0}
    assert accuracy(results, **FAST).value == 0.75


def test_a_task_with_no_trials_is_an_error_not_a_zero() -> None:
    with pytest.raises(ValueError, match="no trials"):
        task_means({"a": []})


def test_pass_hat_k_is_the_chance_that_k_trials_all_pass() -> None:
    assert pass_hat_k(trials(a="TTF"), 3) == 0  # two of three passes is not reliable
    assert pass_hat_k(trials(a="TTT"), 3) == 1
    assert pass_hat_k(trials(a="TTF"), 2) == pytest.approx(1 / 3)  # C(2,2) / C(3,2)
    assert pass_hat_k(trials(a="TTTF"), 2) == pytest.approx(3 / 6)  # C(3,2) / C(4,2)
    assert pass_hat_k(trials(a="TTT", b="TTF", c="FFF"), 3) == pytest.approx(1 / 3)
    assert pass_hat_k(trials(a="TF", b="FF"), 1) == pytest.approx(0.25)  # pass^1 is accuracy


def test_pass_hat_k_needs_at_least_k_trials() -> None:
    with pytest.raises(ValueError, match="fewer than k=3"):
        pass_hat_k(trials(a="TT"), 3)
    with pytest.raises(ValueError):
        pass_hat_k(trials(a="TTT"), 0)


def test_a_bootstrap_of_a_constant_has_no_spread() -> None:
    estimate = accuracy(trials(a="TT", b="TT", c="TT"), **FAST)
    assert (estimate.low, estimate.value, estimate.high) == (1, 1, 1)


def test_a_bootstrap_is_reproducible_and_stays_inside_the_data() -> None:
    results = trials(**{f"t{i}": "T" if i % 3 else "F" for i in range(30)})
    first, again = accuracy(results, **FAST), accuracy(results, **FAST)
    assert first == again
    assert 0 <= first.low <= first.value <= first.high <= 1
    assert first.low < first.high


def test_more_tasks_narrow_the_interval() -> None:
    few = accuracy(trials(**{f"t{i}": "T" if i % 2 else "F" for i in range(10)}), **FAST)
    many = accuracy(trials(**{f"t{i}": "T" if i % 2 else "F" for i in range(200)}), **FAST)
    assert many.high - many.low < few.high - few.low


def test_the_interval_is_the_two_and_a_half_percent_tails_of_the_resampled_statistic() -> None:
    calls = count()  # a statistic that returns 0, 1, 2 ... so the sorted draws are exactly 0..199

    def nth_call(_: Sequence[float]) -> float:
        return float(next(calls))

    assert bootstrap_interval([1.0, 2.0, 3.0], nth_call, resamples=200) == (5.0, 194.0)
    with pytest.raises(ValueError):
        bootstrap_interval([], nth_call)


# ── A/B on the same rows ─────────────────────────────────────────────────────────────────────


def test_a_paired_delta_is_the_mean_per_task_difference_over_the_shared_tasks() -> None:
    new = trials(a="TT", b="TF", c="TT")
    old = trials(a="TF", b="TF", d="TT")  # c and d are in only one run: not paired
    delta = paired_delta(new, old, **FAST)
    assert delta.value == pytest.approx(0.25)  # a: 1.0 - 0.5, b: 0.5 - 0.5
    with pytest.raises(ValueError, match="share no task"):
        paired_delta(trials(a="T"), trials(b="T"))


def test_nearest_rank_is_the_smallest_value_with_that_share_at_or_below() -> None:
    values = [4.0, 1.0, 3.0, 2.0]
    assert nearest_rank(values, 50) == 2.0
    assert nearest_rank(values, 75) == 3.0
    assert nearest_rank(values, 95) == 4.0
    assert nearest_rank(values, 0) == 1.0
    with pytest.raises(ValueError):
        nearest_rank([], 50)


# ── the cost-weighted routing score ──────────────────────────────────────────────────────────


def task(
    expected: Outcome,
    *delivered: Outcome | Unscored | None,
    key: str,
    allowed: tuple[Outcome, ...] = (),
) -> RoutedTask:
    return RoutedTask(key, expected, frozenset(allowed), delivered)


def test_the_matrix_says_over_escalation_is_the_expensive_error() -> None:
    reply = MISROUTE_COST[Outcome.REPLY]
    assert reply[Outcome.PLAN] > MISROUTE_COST[Outcome.PLAN][Outcome.REPLY]
    assert reply[Outcome.PLAN_WITH_MACHINE] == max(reply.values())
    for outcome in Outcome:
        assert MISROUTE_COST[outcome][outcome] == 0
        assert set(MISROUTE_COST[outcome]) == set(Outcome)


def test_a_perfect_run_scores_one_and_the_worst_run_zero() -> None:
    right = [task(Outcome.REPLY, Outcome.REPLY, key="a"), task(Outcome.PLAN, Outcome.PLAN, key="b")]
    assert route_score(right, **FAST).value == 1
    worst = [
        task(Outcome.REPLY, Outcome.PLAN_WITH_MACHINE, key="a"),  # 10 of a worst 10
        task(Outcome.PLAN, Outcome.REPLY, key="b"),  # 4 of a worst 4
    ]
    assert route_score(worst, **FAST).value == 0


def test_the_score_weighs_each_error_by_what_it_costs_against_what_it_could_have_cost() -> None:
    tasks = [task(Outcome.REPLY, Outcome.REPLY, key="a"), task(Outcome.PLAN, Outcome.REPLY, key="b")]
    # one task per outcome, so each weighs 1: 1 - (0 + 4) / (10 + 4)
    assert route_score(tasks, **FAST).value == pytest.approx(1 - 4 / 14)


def test_outcomes_are_weighed_equally_however_many_rows_each_has() -> None:
    many_replies = [task(Outcome.REPLY, Outcome.REPLY, key=f"r{i}") for i in range(9)]
    one_plan_missed = task(Outcome.PLAN, Outcome.REPLY, key="p")
    # reply weighs 1 (nine rows of 1/9), plan weighs 1: 1 - 4 / (10 + 4), not 1 - 4 / (90 + 4)
    assert route_score([*many_replies, one_plan_missed], **FAST).value == pytest.approx(1 - 4 / 14)


def test_rows_that_are_the_same_ask_count_once() -> None:
    same_ask = [
        task(Outcome.REPLY, Outcome.PLAN, key="x"),  # the miss, listed in two books
        task(Outcome.REPLY, Outcome.PLAN, key="x"),
        task(Outcome.REPLY, Outcome.REPLY, key="y"),
    ]
    # reply weights: x 1/2 each, y 1, normalised to sum 1 -> 1/4, 1/4, 1/2: cost 8 * 1/2 over worst 10 * 1
    assert route_score(same_ask, **FAST).value == pytest.approx(1 - 4 / 10)


def test_an_allowed_outcome_costs_nothing() -> None:
    lenient = task(Outcome.REPLY, Outcome.LIGHT_WORK, key="a", allowed=(Outcome.LIGHT_WORK,))
    assert route_score([lenient], **FAST).value == 1


def test_no_ending_and_a_refusal_cost_the_worst_the_row_could_have_got() -> None:
    assert task(Outcome.REPLY, None, key="a").cost() == 10
    assert task(Outcome.PLAN, Unscored.REJECT, key="a").cost() == 4


def test_each_trial_of_a_row_counts_by_its_share() -> None:
    flaky = task(Outcome.REPLY, Outcome.REPLY, Outcome.PLAN, Outcome.REPLY, Outcome.REPLY, key="a")
    assert flaky.cost() == pytest.approx(2)  # one trial in four cost 8


def test_a_score_of_nothing_is_an_error() -> None:
    with pytest.raises(ValueError):
        route_score([])


# ── the gate rule ────────────────────────────────────────────────────────────────────────────


def run(trials_: Trials, cost: float = 1.0, p50: float = 2.0) -> SuiteRun:
    return SuiteRun(trials_, cost, p50)


EVERY_PASS = trials(**{f"t{i}": "T" for i in range(40)})
EVERY_FAIL = trials(**{f"t{i}": "F" for i in range(40)})
HALF = trials(**{f"t{i}": "T" if i % 2 else "F" for i in range(40)})


def test_an_accuracy_drop_whose_interval_is_entirely_below_a_point_fails() -> None:
    flaky = trials(**{f"t{i}": "TF" for i in range(40)})  # every row half as good: none broke outright
    assert regressions(
        run(flaky), run(trials(**{f"t{i}": "TT" for i in range(40)})), SuiteKind.TALK, **FAST
    ) == (Regression.ACCURACY,)


def test_noise_around_the_ratchet_does_not_fail_and_a_gain_never_does() -> None:
    assert regressions(run(EVERY_PASS), run(EVERY_PASS), SuiteKind.TALK, **FAST) == ()
    assert regressions(run(EVERY_PASS), run(HALF), SuiteKind.TALK, **FAST) == ()
    twice = trials(**{f"t{i}": "TT" for i in range(40)})
    one_flaky = trials(**{f"t{i}": "TF" if i == 0 else "TT" for i in range(40)})
    assert regressions(run(one_flaky), run(twice), SuiteKind.TALK, **FAST) == ()  # still passes sometimes


def test_cost_fails_above_five_percent_over_the_ratchet() -> None:
    ratchet = run(EVERY_PASS, cost=1.0)
    assert regressions(run(EVERY_PASS, cost=1.05), ratchet, SuiteKind.TALK, **FAST) == ()
    assert regressions(run(EVERY_PASS, cost=1.06), ratchet, SuiteKind.TALK, **FAST) == (Regression.COST,)


def test_latency_may_slip_half_a_second_for_talk_and_five_for_work() -> None:
    ratchet = run(EVERY_PASS, p50=2.0)
    assert regressions(run(EVERY_PASS, p50=2.5), ratchet, SuiteKind.TALK, **FAST) == ()
    assert regressions(run(EVERY_PASS, p50=2.6), ratchet, SuiteKind.TALK, **FAST) == (Regression.LATENCY,)
    assert regressions(run(EVERY_PASS, p50=7.0), ratchet, SuiteKind.WORK, **FAST) == ()
    assert regressions(run(EVERY_PASS, p50=7.1), ratchet, SuiteKind.WORK, **FAST) == (Regression.LATENCY,)


def test_every_regression_is_reported_not_only_the_first() -> None:
    assert regressions(run(EVERY_FAIL, cost=2.0, p50=9.0), run(EVERY_PASS), SuiteKind.TALK, **FAST) == (
        Regression.ACCURACY,
        Regression.BROKEN,
        Regression.COST,
        Regression.LATENCY,
    )


def test_a_row_that_always_passed_and_now_never_does_fails_where_the_interval_cannot_see_it() -> None:
    one_broke = trials(**{f"t{i}": "F" if i == 0 else "T" for i in range(40)})
    # -2.5 points, but the interval reaches zero: a third of the resamples never draw the broken task
    assert paired_delta(one_broke, EVERY_PASS, **FAST).high == 0
    assert regressions(run(one_broke), run(EVERY_PASS), SuiteKind.TALK, **FAST) == (Regression.BROKEN,)


def test_a_row_the_ratchet_scored_and_the_run_dropped_fails_unless_the_run_said_why() -> None:
    ratchet = trials(**{f"t{i}": "T" for i in range(10)})
    one_left = trials(t0="T")
    assert regressions(run(one_left), run(ratchet), SuiteKind.TALK, **FAST) == (Regression.MISSING,)
    said_why = SuiteRun(one_left, 1.0, 2.0, not_run=frozenset(f"t{i}" for i in range(1, 10)))
    assert regressions(said_why, run(ratchet), SuiteKind.TALK, **FAST) == ()
