# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Header cancel must wake GATE parks (budget/clarify/ask_user), not only STEER.

Cancel wrote the row and poked STEER; a budget park on GATE
slept for 24 h as a zombie workflow while the UI already said cancelled.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.gate import Gate
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.operations import dispatch, gates, steering
from prismoid.operations import tasks as task_ops
from prismoid.schemas.plan import (
    GateAction,
    GateKind,
    GateSpec,
    GateState,
    TaskStatus,
)
from tests.support.cast import member
from tests.support.dispatchers import RecordingDispatcher

pytestmark = pytest.mark.integration


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


@pytest.fixture
async def actor(db: AsyncSession) -> Actor:
    row, _ = await member(db, "Cancel Tester")
    await db.commit()
    return row


async def test_cancel_closes_open_gate_and_wakes_gate_and_steer(
    db: AsyncSession, actor: Actor, recording_dispatcher: RecordingDispatcher
) -> None:
    task = await task_ops.create_task(db, actor, goal="hit the budget then stop")
    # create_task enqueues; put the task into waiting with an open budget-shaped gate
    task_row = await db.get(Task, task.id)
    assert task_row is not None
    task_row.transition(TaskStatus.PLANNING)
    task_row.transition(TaskStatus.RUNNING)
    task_row.transition(TaskStatus.WAITING)
    gate = Gate(
        task_id=task.id,
        kind=GateKind.BUDGET.value,
        state=GateState.OPEN.value,
        spec=GateSpec(kind=GateKind.BUDGET, title="Budget reached", reason="spent").model_dump(mode="json"),
        expires_at=None,
    )
    db.add(gate)
    await db.commit()

    status = await steering.cancel(db, actor, task.id)
    assert status == TaskStatus.CANCELLED.value

    refreshed = await db.get(Task, task.id)
    assert refreshed is not None and refreshed.state is TaskStatus.CANCELLED

    closed = await db.get(Gate, gate.id)
    assert closed is not None
    assert closed.state == GateState.RESOLVED.value
    assert closed.resolution is not None
    assert closed.resolution["action"] == "cancel"

    topics = {(key, topic) for key, _payload, topic in recording_dispatcher.sends}
    task_key = dispatch.task_job_key(task.id)
    assert (task_key, steering.STEER_TOPIC) in topics, "pause park must wake on STEER"
    assert (task_key, "gate") in topics, "budget/clarify park must wake on GATE"

    gate_payloads = [p for k, p, t in recording_dispatcher.sends if t == "gate" and k == task_key]
    assert any(isinstance(p, dict) and p.get("action") == "cancel" for p in gate_payloads)


async def test_cancel_task_on_a_gate_card_cancels_the_task(
    db: AsyncSession, actor: Actor, recording_dispatcher: RecordingDispatcher
) -> None:
    """The card's "Cancel task" closed its gate and nothing else, so a step woken on a
    cancel left, delivery refused the running step, and the task sat in `waiting` for ever."""
    task = await task_ops.create_task(db, actor, goal="stop at the sign-in")
    task_row = await db.get(Task, task.id)
    assert task_row is not None
    task_row.transition(TaskStatus.PLANNING)
    task_row.transition(TaskStatus.RUNNING)
    task_row.transition(TaskStatus.WAITING)
    gate = Gate(
        task_id=task.id,
        kind=GateKind.CONTROL.value,
        state=GateState.OPEN.value,
        spec=GateSpec(kind=GateKind.CONTROL, title="sign in", source="handoff").model_dump(mode="json"),
        expires_at=None,
    )
    db.add(gate)
    await db.commit()

    said = await gates.resolve(db, actor, gate.id, GateAction.CANCEL)
    assert said.action is GateAction.CANCEL

    refreshed = await db.get(Task, task.id)
    assert refreshed is not None and refreshed.state is TaskStatus.CANCELLED
    closed = await db.get(Gate, gate.id)
    assert closed is not None and closed.state == GateState.RESOLVED.value
    payloads = [p for _k, p, t in recording_dispatcher.sends if t == "gate"]
    assert any(isinstance(p, dict) and p.get("action") == "cancel" for p in payloads), "the step wakes"


async def test_a_gate_left_open_on_a_finished_task_is_still_answered_as_cancelled(
    db: AsyncSession, actor: Actor, recording_dispatcher: RecordingDispatcher
) -> None:
    task = await task_ops.create_task(db, actor, goal="finished with a gate left open")
    task_row = await db.get(Task, task.id)
    assert task_row is not None
    task_row.transition(TaskStatus.CANCELLED)
    gate = Gate(
        task_id=task.id,
        kind=GateKind.CONTROL.value,
        state=GateState.OPEN.value,
        spec=GateSpec(kind=GateKind.CONTROL, title="sign in", source="handoff").model_dump(mode="json"),
        expires_at=None,
    )
    db.add(gate)
    await db.commit()

    said = await gates.resolve(db, actor, gate.id, GateAction.CANCEL)
    assert said.action is GateAction.CANCEL
    closed = await db.get(Gate, gate.id)
    assert closed is not None and closed.state == GateState.RESOLVED.value


async def test_cancel_on_already_terminal_is_noop(
    db: AsyncSession, actor: Actor, recording_dispatcher: RecordingDispatcher
) -> None:
    task = await task_ops.create_task(db, actor, goal="already done")
    row = await db.get(Task, task.id)
    assert row is not None
    row.transition(TaskStatus.PLANNING)
    row.transition(TaskStatus.RUNNING)
    row.transition(TaskStatus.CANCELLED)
    await db.commit()

    recording_dispatcher.sends.clear()
    status = await steering.cancel(db, actor, task.id)
    assert status == TaskStatus.CANCELLED.value
    assert recording_dispatcher.sends == []


async def test_cancel_mid_run_gate_wakes_step_key(
    db: AsyncSession, actor: Actor, recording_dispatcher: RecordingDispatcher
) -> None:
    from prismoid.db.models.plan import PlanStep
    from prismoid.schemas.plan import StepKind, StepState

    task = await task_ops.create_task(db, actor, goal="asking mid-run")
    row = await db.get(Task, task.id)
    assert row is not None
    row.transition(TaskStatus.PLANNING)
    row.transition(TaskStatus.RUNNING)
    row.transition(TaskStatus.WAITING)
    step_id = uuid4()
    step = PlanStep(
        id=step_id,
        task_id=task.id,
        title="Ask something",
        kind=StepKind.STEP.value,
        state=StepState.RUNNING.value,
        ordinal=0,
        spec={"agent": "writer", "contract": {"goal": "g", "acceptance": [], "skills": []}},
    )
    db.add(step)
    await db.flush()  # step must exist before the gate's step_id FK
    gate = Gate(
        task_id=task.id,
        step_id=step_id,
        kind=GateKind.INPUT.value,
        state=GateState.OPEN.value,
        spec=GateSpec(kind=GateKind.INPUT, title="One question").model_dump(mode="json"),
    )
    db.add(gate)
    await db.commit()

    await steering.cancel(db, actor, task.id)

    step_key = dispatch.step_job_key(step_id)
    assert any(k == step_key and t == "gate" for k, _p, t in recording_dispatcher.sends)
