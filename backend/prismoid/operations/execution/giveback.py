# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A parked step's frame given back, and the ends a park can come to (a cancel, a kept budget).

A helper that gives a Machine back takes ids, never a live row, and runs after the step session
has committed.
"""

from __future__ import annotations

import contextlib
import uuid

import logfire
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.operations import execution as engine
from prismoid.operations import machines
from prismoid.operations.execution import harvest
from prismoid.schemas.guard import BUDGET_STOP
from prismoid.schemas.park import ParkKind, parked_kind
from prismoid.schemas.plan import (
    StepSpec,
    TaskStatus,
)


async def give_park_back(task_id: uuid.UUID, step_id: uuid.UUID, attempt: int, *, profile: str) -> None:
    """The park's Machine goes back to idle (`release_parked`, on its own session); a park no
    Machine holds has its old frame torn down by name. Best-effort, and on ids alone. The park's
    warm segment is billed in a transaction of its own: every caller has committed or rolled
    back first, so nothing here holds the task's row that the usage record takes."""
    try:
        released = await machines.release_parked(task_id, step_id)
        if released.segment is not None:
            await machines.settle(released.segment)  # never raises
        if released.held:
            return
    except Exception:
        logfire.exception("the park of step {step} could not be given back", step=str(step_id))
    with contextlib.suppress(Exception):
        await machines.discard_legacy(task_id, step_id, attempt, profile=profile)


async def discard_parked_frame(
    session: AsyncSession, task: Task, step: PlanStep, *, profile: str = "base"
) -> None:
    """A cancelled park: a Machine goes back to idle, or just loses its holder if it slept
    (ADR-063); a park no Machine holds - one from before 1b, or a seat's throwaway frame - has its
    frame torn down by its old name. The teardown is best-effort: if it is already gone, good.

    The step session commits first: `release_parked` waits on the
    Machine's row, and a PARK_HOLD sleep's last transaction holds that row while it waits on the
    task's - so this session must not hold the task's row (an event's `next_seq`) meanwhile, a
    wait across two connections Postgres cannot see. The ids are read before anything here can
    touch the session: a commit that fails is rolled back (expiring the rows) and the park is
    STILL given back, on `release_parked`'s own session. Best-effort throughout: a
    caller is about to raise its own outcome, and this must never replace it.

    `profile` must match the one the park created it with (`new_frame`'s driver keys the
    container on task_id AND profile) - a browser-profiled step discarded with the default
    `"base"` would miss its own container and leave it running."""
    task_id, step_id, attempt = task.id, step.id, step.attempt
    try:
        await session.commit()
    except Exception:
        logfire.exception(
            "step {step}: its session could not commit; the park is given back anyway", step=str(step_id)
        )
        with contextlib.suppress(Exception):
            await session.rollback()
    await give_park_back(task_id, step_id, attempt, profile=profile)


async def give_back_a_dead_park(session: AsyncSession, task: Task, step: PlanStep) -> None:
    """A step that will never resume holds nothing: its task finished while it
    was parked, or a twin already settled it. Left held, the task's next turn would wait on a
    Machine that is never given back. Best-effort: the exits that call it (the terminal-task door,
    the twin guard) must return normally, so a failure is logged, the session rolled back, and the
    hold left to the reconciler, which releases a dead one."""
    step_id = step.id  # read first: a rollback expires the rows
    try:
        if await machines.holds(session, task.id, step.id):
            await discard_parked_frame(session, task, step)
    except Exception:
        logfire.exception("step {step}: its dead park could not be given back", step=str(step_id))
        with contextlib.suppress(Exception):
            await session.rollback()


async def cancelled_browser_evidence(session: AsyncSession, task: Task, step: PlanStep) -> None:
    """A browser step parked on a gate the human CANCELLED: bind its still-running frame - the
    Machine's, or a pre-1b park's by its old name (`machines.parked_frame`) - long enough to
    publish the recording (a recording is published on success, failure, cancel or steer - `working`, it
    delivered nothing). A frame that is gone (a Machine that slept) has none to publish.
    Best-effort; the caller gives the park back after."""
    with contextlib.suppress(Exception):
        frame = await machines.parked_frame(session, task, step, profile="browser")
        if frame is None:
            return
        await harvest.record_evidence(
            session, task, step, frame, None, success=False, own_output=False, fact=harvest.quiet_fact
        )


async def read_again(session: AsyncSession, *rows: Task | PlanStep) -> None:
    """Rows a park's give-back expired, read again before anything reads them: a failed commit in
    `_discard_parked_frame` rolls the session back, and an expired attribute cannot lazy-load under
    asyncio. Said when it happens - the commit that failed is the story."""
    for row in rows:
        if sa_inspect(row).expired_attributes:
            logfire.warn("a row the park's give-back expired is read again ({kind})", kind=type(row).__name__)
            await session.refresh(row)


async def end_at_the_budget_gate(session: AsyncSession, task: Task) -> None:
    """A budget gate has no safe default: a decline or a cancel ends the task, and the row
    says so - exactly as at the frontier, where `run_task` marks it cancelled."""
    if not task.is_terminal():
        await engine.advance(session, task_id=task.id, to=TaskStatus.CANCELLED, reason=BUDGET_STOP)
    await session.commit()


async def close_cancelled_park(session: AsyncSession, task_id: uuid.UUID, step_id: uuid.UUID) -> None:
    """The step workflow's cancel exit (a gate resolved `cancel`, or the task's header cancel):
    a BROWSER step's parked frame publishes its recording first; then every park is given back
    - a Machine goes idle, a pre-1b frame is torn down - rather than left for an operator
    sweep, or the task's next turn would find its Machine held by a step that never resumes. A
    step parked on the budget gate between attempts holds nothing and ends the task. Never
    raises."""
    with contextlib.suppress(Exception):
        task = await session.get(Task, task_id)
        step = await session.get(PlanStep, step_id)
        if task is None or step is None:
            return
        if "budget_gate" in (step.resume_state or {}):
            await end_at_the_budget_gate(session, task)
            return
        spec = StepSpec.model_validate(step.spec)
        if spec.agent is None:
            return
        profile = machines.seat_profile(task, spec)
        budget_park = (
            parked_kind(step.resume_state or {}) is ParkKind.BUDGET
        )  # read before the park goes back
        if profile == "browser":
            await cancelled_browser_evidence(session, task, step)
            await session.commit()
        await discard_parked_frame(session, task, step, profile=profile)
        if budget_park:
            # A mid-run budget park cancelled: it ends the task, exactly as
            # the between-attempts budget gate's cancel does. Said, never swallowed, if it cannot.
            try:
                await read_again(session, task)
                await end_at_the_budget_gate(session, task)
            except Exception as exc:
                logfire.warn(
                    "step {step}: its cancelled budget park could not end the task ({error})",
                    step=str(step_id),
                    error=type(exc).__name__,
                )
