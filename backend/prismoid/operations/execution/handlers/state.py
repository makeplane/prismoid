# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What one step's walk carries from rung to rung, and the words a handler answers the driver with.

A handler returns the step's final word (a `str`, `execute_step`'s return), `AGAIN` (go round the
ladder once more), or None (nothing for it to do here: the driver carries on to the next handler).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.artifact import Artifact
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.operations.execution.deferrals import Produced
from prismoid.operations.execution.taps import step_event
from prismoid.operations.turn.commit import Committing
from prismoid.schemas.contract import Contract
from prismoid.schemas.failures import FailureKind
from prismoid.schemas.lead import Fail
from prismoid.schemas.longform import OutlineResult
from prismoid.schemas.outputs import Outcome
from prismoid.schemas.plan import StepSpec, StepState
from prismoid.schemas.swarm import RecordSet, ScatterResult


class Round(enum.Enum):
    AGAIN = enum.auto()


AGAIN: Final = Round.AGAIN

# A run's typed result, as `Produced.outline` holds it: a Lead's failure is read by the fence, never committed.
Outline = OutlineResult | ScatterResult | RecordSet | Outcome | Committing | Fail | None

# What a rung owes without a run: a review of a durable produce, or a kept cap's ending - the
# narration, the artifacts and the typed ending, fed to the outcome handlers with no sandbox.
Injected = tuple[str, list[Artifact], Outcome | None]


@dataclass
class Ran:
    """One rung's result as the outcome handlers read it: what the run (or the review it owed)
    produced, or why it produced nothing (`undelivered`)."""

    narration: str
    produced: list[Artifact]
    outline: Outline
    undelivered: str | None = None
    failure: FailureKind | None = None  # why it produced nothing, when the death named a kind
    attempted: bool = True  # a run just finished (not an injected review): fence 1 reads what it missed
    # The narration is a line on the thread, not the turn's answer: the Lead handed the turn to
    # its plan, and the work's own words are what the turn delivers.
    interim: bool = False

    @classmethod
    def of(cls, outcome: Produced) -> Ran:
        narration, produced, outline = outcome
        return cls(narration, produced, outline)


@dataclass
class Ladder:
    """One step's walk from entry to settle; a rung hands the next its resume, owed review, budget consent
    and gotchas."""

    session: AsyncSession
    task: Task
    step: PlanStep
    spec: StepSpec
    contract: Contract
    seat: bool  # dispatched beside siblings, as the caller said
    seated: bool  # keeps a throwaway frame: dispatched as a seat, or one by the plan's shape
    upstream: list[Artifact]
    gotchas: list[str] = field(default_factory=list)
    failures: list[FailureKind] = field(default_factory=list)  # the typed deaths, rung by rung
    resume: dict[str, object] | None = None
    injected: Injected | None = None
    budget_consented: bool = False
    reentered: bool = False  # recovered mid-run: the next rung re-enters that attempt, not a fresh one
    rung_at: float = 0.0
    no_machine: bool = False
    outgrew_paper: bool = False  # an attempt overflowed its paper frame: the Machine from then on
    backoff: bool = False  # the last attempt's provider failed: the next one waits first
    final: bool = False  # the last attempt met a wall every attempt meets: escalate, no next rung


async def go_round(ladder: Ladder, detail: str) -> Round:
    """Send the step round again in place: retrying → ready → running, on the record and committed.
    Commits; leaves no row lock held."""
    step = ladder.step
    step.transition(StepState.RETRYING)
    await step_event(ladder.session, ladder.task, step, StepState.RETRYING, detail=detail)
    step.transition(StepState.READY)
    step.transition(StepState.RUNNING)
    await ladder.session.commit()
    return AGAIN
