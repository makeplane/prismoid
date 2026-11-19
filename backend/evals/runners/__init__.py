# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Runners: one dataset row in, one `PrismoidTrace` out.

A runner is the only part of an eval that touches the system under test, and it ends in a
`PrismoidTrace`: `live` runs a row as a real task (`PrismoidTrace.from_events`), and `conversation` plays a
conversation row line by line in a bot conversation.

`run_trials` is the loop every runner shares. It runs each row `trials` times under one bound on
how many are in flight, and it is where an infra error is told apart from an agent failure: a
runner raises `InfraError` for what the stack, the provider or the harness did wrong (a dropped
connection, a 5xx, a rate limit), the trial is retried once, and a trial that fails twice is counted and
left out of every score, never scored as the agent's miss. A thread whose earlier turn asked, failed or
never answered is the agent's doing, not infra: the runner raises `ThreadStalled`, the trial is not
retried, and the report names it apart, since the row's own turn never ran. A row that cannot be
run as written (`RowIncomplete`, such as a read with no fixture) is not run: `not_run` moves it to
the report's Not run with its reason, and the suite goes on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from evals.schema import OutcomeRow
from evals.trace import PrismoidTrace


class InfraError(Exception):
    """The stack, the provider or the harness failed, not the agent: worth one retry, never a score."""


class ThreadStalled(Exception):
    """An earlier turn of the row's thread did not deliver, so the row's own turn never ran."""


class RowIncomplete(Exception):
    """The row lacks what its run needs (a fixture): not run, never scored, never retried."""


class TrialTimeout(Exception):
    """The turn did not end within its clock. It carries what the turn had done by then, and the trial
    is scored on that: an agent that never answers has not answered."""

    def __init__(self, trace: PrismoidTrace) -> None:
        super().__init__("the turn did not end in time")
        self.trace = trace


class Runner(Protocol):
    async def __call__(self, row: OutcomeRow, /) -> PrismoidTrace: ...


@dataclass(frozen=True)
class Trial:
    """One run of one row. `trace` is None when the trial failed on infra twice or its thread stalled
    (see `error`)."""

    row: OutcomeRow
    index: int
    trace: PrismoidTrace | None
    error: str = ""
    retried: bool = False  # the first attempt failed on infra, whatever the second did
    timed_out: bool = False
    stalled: bool = False
    incomplete: bool = False


async def run_trials(
    rows: Sequence[OutcomeRow],
    runner: Runner,
    *,
    trials: int,
    concurrency: int,
    on_done: Callable[[Trial], None] = lambda _: None,
) -> list[Trial]:
    """Every row `trials` times, at most `concurrency` at once, trial one of every row first; any
    failure other than the two named ones is a harness bug and stops the run."""
    gate = asyncio.Semaphore(concurrency)

    async def one(row: OutcomeRow, index: int) -> Trial:
        async with gate:
            trial = await _attempt(runner, row, index)
        on_done(trial)
        return trial

    try:
        async with asyncio.TaskGroup() as group:
            pending = [group.create_task(one(row, index)) for index in range(trials) for row in rows]
    except ExceptionGroup as failed:  # the first failure, as itself; the group has cancelled the rest
        raise failed.exceptions[0] from None
    return [task.result() for task in pending]


async def _attempt(runner: Runner, row: OutcomeRow, index: int) -> Trial:
    retried = False
    while True:
        try:
            return Trial(row, index, await runner(row), retried=retried)
        except TrialTimeout as late:
            return Trial(row, index, late.trace, retried=retried, timed_out=True)
        except RowIncomplete as incomplete:
            return Trial(row, index, None, error=str(incomplete), retried=retried, incomplete=True)
        except ThreadStalled as stalled:
            return Trial(row, index, None, error=str(stalled), retried=retried, stalled=True)
        except InfraError as error:
            if retried:
                return Trial(row, index, None, error=str(error), retried=True)
            retried = True


def not_run(trials: Sequence[Trial]) -> tuple[list[Trial], dict[str, str]]:
    """The trials of rows that ran, and why each incomplete row was not run."""
    skipped = {t.row.id: t.error for t in trials if t.incomplete}
    return [t for t in trials if t.row.id not in skipped], skipped
