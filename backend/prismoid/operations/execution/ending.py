# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A turn's typed ending: a delivered turn is `could_not` when its Lead declined, `partial` when any step
ended partly done, else `done`; a failed one is `could_not` when an agent answered that it could not do the
work."""

from __future__ import annotations

import uuid

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.base import utcnow
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.operations import call_log, steering
from prismoid.operations import gates as gate_ops
from prismoid.operations import routines as routine_ops
from prismoid.schemas.events import StepStateChanged, TaskStatusChanged
from prismoid.schemas.ids import ProjectId, TaskId
from prismoid.schemas.lead import LeadEndingKind
from prismoid.schemas.plan import StepState, TaskEnding, TaskStatus
from prismoid.schemas.steering import REJECT_FAILED


async def of_turn(session: AsyncSession, task: Task) -> TaskEnding:
    """Read off the record: this turn's done frames, a Lead's `decline` or a step's typed `partial`."""
    steps = {s.id for s in await PlanStep.for_task(session, task.id) if s.turn_epoch == task.turn_epoch}
    done = [
        e
        for e in await TaskEvent.of_types(session, task.id, ["step.state"])
        if isinstance(e, StepStateChanged) and e.state is StepState.DONE and e.step_id in steps
    ]
    if any(e.ending is LeadEndingKind.DECLINE for e in done):
        return TaskEnding.COULD_NOT
    return TaskEnding.PARTIAL if any(e.outcome == "partial" for e in done) else TaskEnding.DONE


async def fail(
    session: AsyncSession,
    task_id: uuid.UUID,
    reason: str,
    *,
    turn: int | None = None,
    ended: TaskEnding | None = None,
) -> None:
    """A failed task is a **state**, not a crashed worker.

    The user gets a status they can see and a reason they can read - not a job that vanished.
    `turn` is the stale-writer guard: only the turn that owns the run may fail it. `ended` is
    `could_not` when an agent answered that it could not do the work.
    """
    task = await session.get(Task, task_id)
    if task is None or task.is_terminal():
        return
    if turn is not None and task.turn != turn:
        logfire.warn(
            "stale turn-{turn} workflow tried to fail task {t} (now turn {now}); refused",
            turn=turn,
            t=str(task_id),
            now=task.turn,
        )
        return

    # **A failed task closes its book too** (the deliver() law). A task that fails -
    # or is sibling-cancelled mid-wave - while a question is parked would otherwise leave a
    # zombie gate answerable forever: `prismoid gates` and the web dock render a question over a
    # dead task. Expire the open gates as DECLINE (the same wire shape a Skip writes), seq'd
    # before `task.failed`, exactly as delivery does.

    await gate_ops.expire_open(session, task)

    task.transition(TaskStatus.FAILED)
    await call_log.forget(session, task.id)
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        TaskStatusChanged(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            status=TaskStatus.FAILED,
            reason=reason[:2000],
            ending=ended,
        ),
    )
    # What never applied says so, to everyone - in this transaction, after the failure.
    await steering.reject_unapplied(session, task, REJECT_FAILED)
    if task.origin_routine_id is not None and task.turn == 1:
        # The routine's failure ladder counts this settle: notify per policy,
        # auto-pause at the threshold - and if an escalation already carried the diagnosis,
        # the routine does not say the same thing twice. Turn 1 only: a failed FOLLOW-UP is
        # the owner's own turn, not a routine run.

        await routine_ops.on_settled(session, task, failed=True, summary=reason[:2000])
