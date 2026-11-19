# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The metrics every report quotes, defined once.

Pure functions over per-task trial results; nothing here reads a trace, a row file or a clock.
The numbers are reproducible: every resampling seeds its own generator, so the same trials give the
same interval on every machine.

A suite's results are `Trials`: task id -> the pass/fail of each trial of that task. Infra errors
are not agent failures, so a runner leaves them out before it gets here.

* `accuracy`: mean over tasks of the mean over trials, with a 95% bootstrap interval over *tasks*
  (the unit that was sampled), not trials.
* `pass_hat_k`: the chance that k trials all pass, by the unbiased C(c,k)/C(n,k) estimator.
* `paired_delta`: A/B on the same rows. A change wins only if the paired interval excludes zero.
* `route_score`: the cost-weighted routing score. Over-escalation is the expensive error, so errors
  are weighed by `MISROUTE_COST`, not counted.
* `regressions`: the gate rule the ratchet applies. No gate is loosened. The interval alone cannot see a
  few rows break on a small suite, so a row that always passed and now never does fails on its own,
  and so does a row the ratchet scored that the run dropped.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from evals.schema import Outcome, OutcomeRow, Unscored

RESAMPLES = 10_000
CONFIDENCE = 0.95
SEED = 0

type Trials = Mapping[str, Sequence[bool]]


@dataclass(frozen=True)
class Estimate:
    """A number and the 95% interval it was bootstrapped to."""

    value: float
    low: float
    high: float


# ── bootstrap ────────────────────────────────────────────────────────────────────────────────


def bootstrap_interval[T](
    items: Sequence[T],
    statistic: Callable[[Sequence[T]], float],
    *,
    resamples: int = RESAMPLES,
    confidence: float = CONFIDENCE,
    seed: int = SEED,
) -> tuple[float, float]:
    """The percentile interval of `statistic` over resamples of `items` drawn with replacement."""
    if not items:
        raise ValueError("a bootstrap needs at least one item")
    rng = random.Random(seed)
    draws = sorted(statistic(rng.choices(items, k=len(items))) for _ in range(resamples))
    cut = round((1 - confidence) / 2 * resamples)
    return draws[cut], draws[resamples - 1 - cut]


def _mean_estimate(values: Sequence[float], *, resamples: int, seed: int) -> Estimate:
    low, high = bootstrap_interval(values, statistics.fmean, resamples=resamples, seed=seed)
    return Estimate(statistics.fmean(values), low, high)


# ── accuracy, reliability, A/B ───────────────────────────────────────────────────────────────


def task_means(trials: Trials) -> dict[str, float]:
    """Each task's pass rate over its trials."""
    means: dict[str, float] = {}
    for task, passes in trials.items():
        if not passes:
            raise ValueError(f"task {task!r} has no trials")
        means[task] = sum(passes) / len(passes)
    return means


def accuracy(trials: Trials, *, resamples: int = RESAMPLES, seed: int = SEED) -> Estimate:
    """Pass@1 averaged over trials, then over tasks, with a bootstrap interval over tasks."""
    return _mean_estimate(list(task_means(trials).values()), resamples=resamples, seed=seed)


def pass_hat_k(trials: Trials, k: int) -> float:
    """The chance that k trials of a task all pass, averaged over tasks: C(c,k)/C(n,k) per task."""
    if k < 1:
        raise ValueError("k must be at least 1")
    if not trials:
        raise ValueError("pass^k needs at least one task")
    chances: list[float] = []
    for task, passes in trials.items():
        if len(passes) < k:
            raise ValueError(f"task {task!r} has {len(passes)} trials, fewer than k={k}")
        chances.append(math.comb(sum(passes), k) / math.comb(len(passes), k))
    return statistics.fmean(chances)


def _shared(new: Trials, old: Trials) -> list[str]:
    tasks = sorted(new.keys() & old.keys())
    if not tasks:
        raise ValueError("the two runs share no task")
    return tasks


def paired_delta(new: Trials, old: Trials, *, resamples: int = RESAMPLES, seed: int = SEED) -> Estimate:
    """Mean per-task pass-rate difference (new - old) over the tasks both runs hold, bootstrapped."""
    new_means, old_means = task_means(new), task_means(old)
    return _mean_estimate(
        [new_means[task] - old_means[task] for task in _shared(new, old)], resamples=resamples, seed=seed
    )


def nearest_rank(values: Sequence[float], percent: float) -> float:
    """The nearest-rank percentile: the smallest value with at least `percent`% of the data at or below."""
    if not values:
        raise ValueError("a percentile needs at least one value")
    ordered = sorted(values)
    return ordered[max(math.ceil(percent / 100 * len(ordered)), 1) - 1]


# ── the cost-weighted routing score ──────────────────────────────────────────────────────────

_OUTCOMES = tuple(Outcome)

# Expected (rows) by delivered (columns) in `Outcome` order; a reasoned prior a human retunes, never a fit.
_MISROUTE_ROWS = (
    (0, 1, 8, 10, 3, 3),  # reply
    (5, 0, 6, 8, 3, 3),  # light_work
    (4, 4, 0, 2, 2, 2),  # plan
    (6, 5, 4, 0, 2, 2),  # plan_with_machine
    (3, 4, 6, 8, 0, 2),  # ask
    (3, 3, 5, 6, 2, 0),  # propose
)
MISROUTE_COST: Mapping[Outcome, Mapping[Outcome, int]] = {
    expected: dict(zip(_OUTCOMES, costs, strict=True))
    for expected, costs in zip(_OUTCOMES, _MISROUTE_ROWS, strict=True)
}


@dataclass(frozen=True)
class RoutedTask:
    """One scored row and what each of its trials delivered."""

    dedupe_key: str
    expected: Outcome
    allowed: frozenset[Outcome]
    delivered: Sequence[Outcome | Unscored | None]

    @classmethod
    def of(cls, row: OutcomeRow, delivered: Sequence[Outcome | Unscored | None]) -> RoutedTask:
        if not isinstance(row.expected_outcome, Outcome):
            raise ValueError(f"row {row.id} is not scored: it expects {row.expected_outcome.value!r}")
        return cls(row.dedupe_key, row.expected_outcome, frozenset(row.allowed_outcomes), delivered)

    def cost(self) -> float:
        """Mean misroute cost over the trials."""
        return statistics.fmean(self._cost_of(got) for got in self.delivered)

    def worst(self) -> int:
        return max(MISROUTE_COST[self.expected].values())

    def _cost_of(self, got: Outcome | Unscored | None) -> int:
        if got is self.expected or got in self.allowed:
            return 0
        if isinstance(got, Outcome):
            return MISROUTE_COST[self.expected][got]
        return self.worst()  # no ending, or a refusal: the worst the row could have got


def route_score(tasks: Sequence[RoutedTask], *, resamples: int = RESAMPLES, seed: int = SEED) -> Estimate:
    """RouteScore_cw = 1 - sum(w * cost) / sum(w * worst), w = 1/(same-ask rows) scaled to sum to one
    per expected outcome, fixed before the bootstrap resamples tasks."""
    if not tasks:
        raise ValueError("a route score needs at least one task")
    group_size = {key: sum(t.dedupe_key == key for t in tasks) for key in {t.dedupe_key for t in tasks}}
    stratum_weight = {
        outcome: sum(1 / group_size[t.dedupe_key] for t in tasks if t.expected is outcome)
        for outcome in {t.expected for t in tasks}
    }
    terms: list[tuple[float, float]] = []
    for task in tasks:
        weight = 1 / group_size[task.dedupe_key] / stratum_weight[task.expected]
        terms.append((weight * task.cost(), weight * task.worst()))

    def score(sample: Sequence[tuple[float, float]]) -> float:
        return 1 - sum(cost for cost, _ in sample) / sum(worst for _, worst in sample)

    low, high = bootstrap_interval(terms, score, resamples=resamples, seed=seed)
    return Estimate(score(terms), low, high)


# ── the gate rule ────────────────────────────────────────────────────────────────────────────


class SuiteKind(StrEnum):
    """How much slower a suite may get: talk answers in seconds, work runs for minutes."""

    TALK = "talk"
    WORK = "work"


class Regression(StrEnum):
    ACCURACY = "accuracy"
    BROKEN = "broken rows"
    MISSING = "missing rows"
    COST = "cost"
    LATENCY = "latency"


ACCURACY_FLOOR = -0.01  # a paired interval entirely below this many points of accuracy fails
COST_CEILING = 1.05  # spend above this multiple of the ratchet fails
LATENCY_SLACK_S: Mapping[SuiteKind, float] = {SuiteKind.TALK: 0.5, SuiteKind.WORK: 5.0}
SLACK_PASS_K = 3


@dataclass(frozen=True)
class SuiteRun:
    """What the gate compares: per-task results, the cost statistic, the p50 latency, and the tasks the
    runner said it could not set up."""

    trials: Trials
    cost_usd: float
    p50_s: float
    not_run: frozenset[str] = frozenset()


def missing_rows(run: SuiteRun, ratchet: SuiteRun) -> frozenset[str]:
    """Tasks the ratchet scored that the run neither scored nor said it could not run."""
    return frozenset(ratchet.trials.keys() - run.trials.keys() - run.not_run)


def broken_rows(run: SuiteRun, ratchet: SuiteRun) -> frozenset[str]:
    """Shared tasks that passed every trial in the ratchet and none in the run."""
    shared = run.trials.keys() & ratchet.trials.keys()
    return frozenset(t for t in shared if all(ratchet.trials[t]) and not any(run.trials[t]))


def regressions(
    run: SuiteRun, ratchet: SuiteRun, kind: SuiteKind, *, resamples: int = RESAMPLES, seed: int = SEED
) -> tuple[Regression, ...]:
    """Where `run` is worse than the ratchet (empty passes): accuracy's paired interval below one point,
    a broken or missing row, cost above 1.05x, p50 above the ratchet plus the suite kind's slack."""
    failed: list[Regression] = []
    shared = run.trials.keys() & ratchet.trials.keys()
    if (
        shared
        and paired_delta(run.trials, ratchet.trials, resamples=resamples, seed=seed).high < ACCURACY_FLOOR
    ):
        failed.append(Regression.ACCURACY)
    if broken_rows(run, ratchet):
        failed.append(Regression.BROKEN)
    if missing_rows(run, ratchet):
        failed.append(Regression.MISSING)
    if run.cost_usd > ratchet.cost_usd * COST_CEILING:
        failed.append(Regression.COST)
    if run.p50_s > ratchet.p50_s + LATENCY_SLACK_S[kind]:
        failed.append(Regression.LATENCY)
    return tuple(failed)
