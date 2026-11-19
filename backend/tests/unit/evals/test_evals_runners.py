# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The trial loop every runner shares: bounded, ordered, and honest about what failed and why."""

from __future__ import annotations

import asyncio

import pytest

from evals.runners import InfraError, RowIncomplete, ThreadStalled, Trial, TrialTimeout, not_run, run_trials
from evals.schema import Outcome, OutcomeRow
from evals.trace import PrismoidTrace


def rows(count: int) -> list[OutcomeRow]:
    return [OutcomeRow(id=f"r{n}", message="m", expected_outcome=Outcome.REPLY) for n in range(count)]


REPLY = PrismoidTrace(outcome=Outcome.REPLY, delivered=True)


class Scripted:
    """A runner that answers each row from a script of what its attempts do, in order."""

    def __init__(self, script: dict[str, list[PrismoidTrace | Exception]] | None = None) -> None:
        self.script = script or {}
        self.started: list[str] = []
        self.in_flight = 0
        self.most_in_flight = 0

    async def __call__(self, row: OutcomeRow, /) -> PrismoidTrace:
        self.started.append(row.id)
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
            step = self.script.get(row.id, [REPLY]).pop(0)
        finally:
            self.in_flight -= 1
        if isinstance(step, Exception):
            raise step
        return step


async def test_an_infra_error_is_retried_once_and_the_trial_still_counts() -> None:
    runner = Scripted({"r0": [InfraError("reset"), REPLY]})
    (trial,) = await run_trials(rows(1), runner, trials=1, concurrency=1)
    assert trial.trace == REPLY
    assert trial.retried
    assert trial.error == ""


async def test_an_infra_error_twice_is_counted_and_has_no_trace_to_score() -> None:
    runner = Scripted({"r0": [InfraError("reset"), InfraError("reset again")]})
    (trial,) = await run_trials(rows(1), runner, trials=1, concurrency=1)
    assert trial.trace is None
    assert trial.error == "reset again"
    assert trial.retried
    assert runner.started == ["r0", "r0"]


async def test_a_timeout_is_not_retried_and_keeps_what_the_turn_did() -> None:
    partial = PrismoidTrace(outcome=None)
    runner = Scripted({"r0": [TrialTimeout(partial)]})
    (trial,) = await run_trials(rows(1), runner, trials=1, concurrency=1)
    assert trial.timed_out
    assert trial.trace == partial
    assert not trial.retried
    assert runner.started == ["r0"]


async def test_a_stalled_thread_is_not_retried_and_has_no_trace_to_score() -> None:
    runner = Scripted({"r0": [ThreadStalled("turn 1 of the thread did not deliver")]})
    (trial,) = await run_trials(rows(1), runner, trials=1, concurrency=1)
    assert trial.stalled and trial.trace is None
    assert trial.error == "turn 1 of the thread did not deliver"
    assert not trial.retried
    assert runner.started == ["r0"]  # asked once: a retry would spend twice on the agent's own answer


async def test_a_bug_in_the_harness_stops_the_run_as_itself_and_the_other_trials_stop_with_it() -> None:
    gate = asyncio.Event()
    cancelled: list[str] = []

    async def runner(row: OutcomeRow, /) -> PrismoidTrace:
        if row.id == "r0":
            raise KeyError("a field the API no longer sends")
        try:
            await gate.wait()
        except asyncio.CancelledError:
            cancelled.append(row.id)
            raise
        return REPLY

    with pytest.raises(KeyError):
        await run_trials(rows(3), runner, trials=1, concurrency=3)
    assert sorted(cancelled) == ["r1", "r2"]  # nothing keeps spending after the run is dead


async def test_no_more_trials_run_at_once_than_the_bound_and_the_bound_is_used() -> None:
    runner = Scripted()
    await run_trials(rows(8), runner, trials=1, concurrency=3)
    assert runner.most_in_flight == 3


async def test_every_row_gets_its_first_trial_before_any_gets_its_second() -> None:
    runner = Scripted()
    trials = await run_trials(rows(3), runner, trials=2, concurrency=1)
    assert runner.started == ["r0", "r1", "r2", "r0", "r1", "r2"]
    assert [(t.row.id, t.index) for t in trials] == [(f"r{n}", i) for i in range(2) for n in range(3)]


async def test_each_finished_trial_is_reported_as_it_finishes() -> None:
    done: list[Trial] = []
    await run_trials(rows(2), Scripted(), trials=2, concurrency=2, on_done=done.append)
    assert len(done) == 4


async def test_an_incomplete_row_is_not_run_with_its_reason_and_the_suite_goes_on() -> None:
    runner = Scripted({"r0": [RowIncomplete("no fixture for web_search")]})
    trials = await run_trials(rows(2), runner, trials=1, concurrency=1)
    kept, skipped = not_run(trials)
    assert [t.row.id for t in kept] == ["r1"]
    assert skipped == {"r0": "no fixture for web_search"}
    assert runner.started == ["r0", "r1"]  # never retried
