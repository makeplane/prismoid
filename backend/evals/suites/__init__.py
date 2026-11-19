# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Suites: one capability's dataset rows scored on what the person got, with time and $ beside it.

A suite registered in `run.SUITES` with `scored=` picks the rows a live run submits and scores every
turn from its durable record (and, for a deliverable, its signed receipt), which `RecordingRunner` keeps
beside the trace its trial holds. Its pass per turn is the trial's pass in the report, and its table is in it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol

from evals.runners import TrialTimeout
from evals.runners.live import LiveRunner
from evals.schema import OutcomeRow
from evals.trace import PrismoidTrace, events_from_payloads
from prismoid.schemas.events import DurableEvent

# The signed verification of one artifact as the API serves it, by artifact id; None when none came.
type Receipts = Callable[[str], Awaitable[dict[str, Any] | None]]


# The events of the turn the current trial just played; each trial runs in its own task, so its own context.
_played: ContextVar[list[DurableEvent] | None] = ContextVar("_played", default=None)


@dataclass(frozen=True)
class RecordedTurn:
    row: OutcomeRow
    events: list[DurableEvent]
    trace: PrismoidTrace | None = None  # the very trace the trial holds, so its pass lands on that trial


@dataclass(frozen=True)
class Scorecard:
    """A suite's verdict on its turns: the table for the report, and its pass for each turn, in order."""

    table: str
    passed: tuple[bool, ...]


class Scored(Protocol):
    def select(self, rows: Sequence[OutcomeRow]) -> list[OutcomeRow]: ...

    async def score(self, turns: Sequence[RecordedTurn], receipts: Receipts) -> Scorecard: ...


class RecordingRunner(LiveRunner):
    """The live runner, keeping every played turn's durable record, with its trial's trace, for a suite's scorer."""

    turns: list[RecordedTurn]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.turns = []

    async def __call__(self, row: OutcomeRow, /) -> PrismoidTrace:
        _played.set(None)
        try:
            trace = await super().__call__(row)
        except TrialTimeout as late:
            self._keep(row, late.trace)
            raise
        self._keep(row, trace)
        return trace

    async def _play_thread(self, row: OutcomeRow, created: list[str]) -> Any:
        turn = await super()._play_thread(row, created)
        if turn is not None:
            _played.set(events_from_payloads(turn.payloads))
        return turn

    def _keep(self, row: OutcomeRow, trace: PrismoidTrace) -> None:
        if (events := _played.get()) is not None:
            self.turns.append(RecordedTurn(row=row, events=events, trace=trace))
