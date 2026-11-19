# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What never ran, when a task stops: its unapplied steers are rejected and its never-started steps
settle `skipped`, each saying so on the record, in the transaction that stops the task; and the stop's
account (`account`), what the turn finished first, which the cancel's own frame carries.
`steering.py` re-exports `reject_unapplied`."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.base import utcnow
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.steer import Steer
from prismoid.db.models.task import Task
from prismoid.schemas.events import ArtifactProduced, SteerRejected, StepStateChanged
from prismoid.schemas.ids import ProjectId, StepId, TaskId
from prismoid.schemas.plan import StepState, TaskStatus
from prismoid.schemas.steering import SteerState

# The seat that decides a turn: its step is the turn's deciding, never a piece of the work it counts.
_LEAD = "lead"


async def reject_unapplied(session: AsyncSession, task: Task, reason: str) -> None:
    """The task stopped before these steers applied: each says so, to everyone, oldest
    first - and, once the task has ended, the steps it never started settle too
    (`settle_unstarted`). The caller holds the task row and commits."""
    rows = await Steer.unapplied(session, task.id)
    moved = await Steer.settle(
        session, task.id, [r.command_id for r in rows], to=SteerState.REJECTED, reason=reason
    )
    for row in sorted(moved, key=lambda r: r.epoch):
        seq = await task.next_seq(session)
        await TaskEvent.append(
            session,
            SteerRejected(
                seq=seq,
                task_id=TaskId(task.id),
                project_id=ProjectId(task.project_id) if task.project_id else None,
                at=utcnow(),
                command_id=row.command_id,
                reason=reason,
            ),
        )
    if task.is_terminal():
        await settle_unstarted(session, task)


def ended_said(status: str) -> str:
    """How a stopped task is said in a step's detail."""
    return "the task failed" if status == TaskStatus.FAILED.value else f"the task was {status}"


async def settle_unstarted(session: AsyncSession, task: Task) -> None:
    """The steps a stopped task never started settle `skipped`, saying why: `pending`, and `ready`
    with no attempt yet (a queued run that reaches its door finds the task ended and ejects). A
    step that has run is its own ladder's to settle - a paced rung, a running one (its safe
    point stops it). A settled step is left as it is. The caller holds the task row and commits."""
    why = f"not started: {ended_said(task.status)}"
    for step in await PlanStep.for_task(session, task.id):
        state = step.step_state
        if not (state is StepState.PENDING or (state is StepState.READY and step.attempt == 0)):
            continue
        step.transition(StepState.SKIPPED)
        await TaskEvent.append(
            session,
            StepStateChanged(
                seq=await task.next_seq(session),
                task_id=TaskId(task.id),
                step_id=StepId(step.id),
                project_id=ProjectId(task.project_id) if task.project_id else None,
                at=utcnow(),
                state=StepState.SKIPPED,
                title=step.title,
                agent=step.spec.get("agent"),
                attempt=step.attempt,
                detail=why,
                model_ref=step.spec.get("model_ref"),
            ),
        )


async def account(session: AsyncSession, task: Task) -> str:
    """What this turn finished before it stopped, the files each step published, and what it never did,
    in markdown built from the record (no model call); a step the stop skipped reads as never started."""
    steps = [
        s
        for s in await PlanStep.for_task(session, task.id)
        if s.turn_epoch == task.turn_epoch and s.spec.get("agent") != _LEAD
    ]
    if not steps:
        return "Stopped before any work started."
    files: dict[str, list[str]] = {}
    for e in await TaskEvent.of_types(session, task.id, ["artifact.produced"]):
        if isinstance(e, ArtifactProduced) and e.step_id is not None and e.role == "deliverable":
            files.setdefault(str(e.step_id), []).append(e.filename or e.title)
    done = [s for s in steps if s.step_state is StepState.DONE]
    lines = [f"Stopped after {len(done)} of {len(steps)} steps."]
    for step in steps:
        made = files.get(str(step.id), [])
        lines.append(f"- {_said(step)}: {step.title}" + (f" ({', '.join(made)})" if made else ""))
    return "\n".join(lines)


def _said(step: PlanStep) -> str:
    """A step's state as the stop's account says it."""
    state = step.step_state
    if state is StepState.DONE:
        return "Done"
    if state in (StepState.PENDING, StepState.SKIPPED) or (state is StepState.READY and step.attempt == 0):
        return "Not started"
    return "Stopped mid-way"
