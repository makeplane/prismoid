# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A step found at the door in a state a crash, a park or a person left it in: its open gate
re-parked or its recorded answer hydrated, a device step resumed, a run that died mid-flight
netted out of every cap, a review owed again.
"""

from __future__ import annotations

import uuid

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.artifact import Artifact
from prismoid.db.models.gate import Gate
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.errors import PolicyDenied
from prismoid.operations import execution as engine
from prismoid.operations.execution import wake
from prismoid.operations.execution.device import Cut, DeviceResume, on_device, owed_state
from prismoid.operations.execution.record import WOKE, hand_back, owed_gotchas, woke
from prismoid.operations.execution.run import answer_logged
from prismoid.operations.execution.taps import step_event
from prismoid.schemas.device_away import device_label
from prismoid.schemas.outputs import Outcome
from prismoid.schemas.park import PARK, park_of
from prismoid.schemas.plan import StepState, TaskStatus


async def recover_parked_gate(
    session: AsyncSession, task: Task, step: PlanStep, record: list[str]
) -> str | None:
    """What a step's gate row says at the door: re-park on an open gate, hydrate the resume from a resolved
    one whose answer never reached the run, the words of a task that ended, or None to carry on."""
    # A device step parks from `verifying` too (`_park_device_away`), and its gate is
    # consulted the same way.
    parks_from = (StepState.RUNNING, StepState.VERIFYING) if on_device(task) else (StepState.RUNNING,)
    if step.step_state not in parks_from or (
        step.resume_state
        and (
            "answer" in step.resume_state
            or "approval_action" in step.resume_state
            or "connect_result" in step.resume_state
            or "pause_resumed" in step.resume_state
            or "budget_action" in step.resume_state
            or "budget_answer" in step.resume_state
            or "device_resumed" in step.resume_state
        )
    ):
        return None

    parked_gate = await Gate.latest_for_step(session, step.id)
    # A takeover gate still open after a death between the park's commit and its race settle:
    # its lease may have ended meanwhile with nobody left to answer it. Settle it
    # now - DBOS-free and idempotent - and continue the same run instead of re-parking.
    if parked_gate is not None and parked_gate.is_open():
        if await wake.settle_parked(task.id, step, parked_gate):
            ended = await wake.ended_meanwhile(session, task, step)
            if ended is not None:
                return ended
        # Re-read the row either way: the settle closes it on its own session, and so may a
        # twin whose settle won - the in-memory copy here still says open.
        await session.refresh(parked_gate)
    if parked_gate is not None and parked_gate.is_open():
        logfire.info("step {s} recovered onto its still-open gate - re-parking", s=str(step.id))
        outcome = hand_back(step, f"park:{parked_gate.id}")
        await session.commit()
        return outcome
    if (
        parked_gate is not None
        and parked_gate.resolution is not None
        and (
            f"park:{parked_gate.id}" not in record[: woke(step)]
            or (_parked_on(step) == parked_gate.id and not await answer_logged(task, step, parked_gate.id))
        )
    ):
        # The gate row is the durable truth: map its resolution into the park's
        # channel through the ONE hydration - the same mapping the live wake path uses.
        await wake.hydrate_resume(session, step, parked_gate.resolution)
        step.resume_state = {**(step.resume_state or {}), WOKE: len(record)}
    return None


def _parked_on(step: PlanStep) -> uuid.UUID | None:
    """The gate of the park the row still holds (kept through its wake until the run replaces it)."""
    state = step.resume_state or {}
    return park_of(state).gate_id if PARK in state else None


async def resumed_on_device(session: AsyncSession, task: Task, step: PlanStep) -> DeviceResume:
    """**A person resumed a step that waited for its device**. An away park that
    held a resume (a person's answer, a pause lifted) goes on with that same run; a step
    parked in its review resumes at the review (its produce is durable); any other
    starts a fresh attempt - an infra rung, so it nets out of every cap - told
    what the crash cut off (`cut`)."""
    state = step.resume_state or {}
    away = state.get("device_away")
    away = away if isinstance(away, dict) else {}
    held, cut = away.get("resume"), Cut.restored(away)
    gotchas, _ = owed_gotchas(away)  # why the attempt before the park failed
    cancelled = bool(state.get("cancelled"))
    fresh = not isinstance(held, dict) and step.step_state is not StepState.VERIFYING
    step.resume_state = owed_state(step, cut if fresh else None)  # owed only to a fresh attempt's prompt
    if cancelled:
        await session.commit()
        raise PolicyDenied("the task was cancelled while its step waited for its device")
    resume: dict[str, object] | None = None
    at_review = False
    if isinstance(held, dict):
        resume = held
        detail = f"resumed: {device_label(away.get('name'))} is back"
        await step_event(session, task, step, StepState.RUNNING, detail=detail)
    elif step.step_state is StepState.VERIFYING:
        at_review = True
    else:
        step.infra_attempts += 1
    await session.commit()
    return DeviceResume(gotchas, resume, at_review)


async def recovered_mid_flight(
    session: AsyncSession, task: Task, step: PlanStep, *, same_attempt: bool
) -> None:
    """A step found in flight: the previous worker died inside it, and it goes round again.
    `same_attempt`: it re-enters the attempt it died in (a review owed, or a run its call log replays)."""
    logfire.warn(
        "step {step_id} recovered mid-flight - the previous worker died inside it",
        step_id=str(step.id),
    )
    # An infrastructure death is not a model failure: the fresh sandbox below
    # bumps `attempt` (frame-name uniqueness), and this bump cancels it out of every retry
    # cap - a worker restart must never spend the user's ladder budget.
    #
    # A re-entry of the same attempt is the exception: a VERIFYING one owes only the review, and
    # a RUNNING one replays its call log, so neither bumps `attempt` (ADR-089). Bumping infra here
    # would DECREMENT `model_attempts()` and hand out an extra billed attempt. Infra is bumped only
    # where a sandbox is actually re-bought - the empty-artifacts branch of `review_owed`.
    if not same_attempt:
        step.infra_attempts += 1
    # And un-park: if the task sat WAITING for a question this step no longer holds (no
    # open gate - the re-park path above already returned for a live one), nothing is
    # being waited for. Leaving it WAITING strands delivery on waiting → delivered.
    if task.state is TaskStatus.WAITING and not await Gate.open_for_task(session, task.id):
        await engine.advance(session, task_id=task.id, to=TaskStatus.RUNNING)
        await session.commit()


async def review_owed(
    session: AsyncSession, step: PlanStep
) -> tuple[str, list[Artifact], Outcome | None] | None:
    """The injected outcome of a VERIFYING re-entry, or None when the harvest left nothing
    to review: the step then goes round on a fresh attempt."""
    # Live output only: what fence 1 superseded on an earlier attempt is not under review.
    produced_rows = [a for a in await Artifact.for_step(session, step.id) if a.role != "superseded"]
    if produced_rows:
        return "(recovered at review - the produce was already durable)", produced_rows, None
    logfire.warn("step {s} recovered VERIFYING with no artifacts - fresh attempt", s=str(step.id))
    step.infra_attempts += 1  # a re-bought sandbox IS an infra rung - the
    step.transition(StepState.RETRYING)  # `attempt` bump in the loop nets out, so
    step.transition(StepState.READY)  # model_attempts() stays flat across a kill-during-QA
    step.transition(StepState.RUNNING)
    return None
