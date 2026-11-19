# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The approval gate (ADR-054): `GateKind.APPROVE` instantiated at last.

One gate per approval batch; `questions=None`; the evidence in `spec.reason` as one block per
call; deny-on-timeout STRUCTURAL (no questions ⇒ the sweep's proceed-with-defaults arm ⇒
DECLINE ⇒ the resume maps to ToolDenied); the inbox knocked in the same transaction; and the
two step budgets (ask vs approval) counting past each other, never through each other.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.gate import Gate
from prismoid.db.models.inbox import InboxItem, InboxKind
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.operations import execution, gates
from prismoid.operations import tasks as task_ops
from prismoid.schemas.ids import StepId
from prismoid.schemas.plan import GateKind, GateSpec, GateState
from tests.support.asks import question_set
from tests.support.cast import member

pytestmark = pytest.mark.integration


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


@pytest.fixture
async def actor(db: AsyncSession) -> Actor:
    row, _ = await member(db, "Approver")
    await db.commit()
    return row


async def _task_and_step(db: AsyncSession, actor: Actor) -> tuple[Task, PlanStep]:
    task = await task_ops.create_task(db, actor, goal="send the weekly summary")
    row = await db.get(Task, task.id)
    assert row is not None
    step = PlanStep(task_id=row.id, ordinal=0, title="send it")
    db.add(step)
    await db.flush()
    return row, step


def _items(n: int = 1) -> list[tuple[str, str, str, str]]:
    return [(f"call_{i}", "Gmail", "gmail_send_email", f"body: hello {i}\nto: a@b.c") for i in range(n)]


async def test_open_approval_builds_the_gate_the_knock_and_the_event(
    db: AsyncSession, actor: Actor, silent_dispatcher: object
) -> None:
    task, step = await _task_and_step(db, actor)
    gate = await gates.open_approval(db, task, StepId(step.id), _items(1))
    await db.commit()

    spec = GateSpec.model_validate(gate.spec)
    assert spec.kind is GateKind.APPROVE and spec.questions is None
    assert spec.title == "Allow Gmail: gmail_send_email?"
    assert "to: a@b.c" in spec.reason, "the evidence rides the gate, for the owner deciding"
    assert gate.expires_at is not None and gate.expires_at > utcnow() + dt.timedelta(hours=23)

    knock = await db.scalar(select(InboxItem).where(InboxItem.gate_id == gate.id))
    assert knock is not None and knock.kind == InboxKind.GATE_OPENED.value
    assert knock.title == "Approval needed: Gmail"

    from prismoid.schemas.events import GateOpened

    events = await TaskEvent.replay(db, task.id)
    opened = [e for e in events if isinstance(e, GateOpened)]
    assert opened and opened[-1].kind is GateKind.APPROVE
    assert "to: a@b.c" in opened[-1].reason, "the stream carries the same evidence"


async def test_a_batch_gets_one_gate_with_every_block(
    db: AsyncSession, actor: Actor, silent_dispatcher: object
) -> None:
    task, step = await _task_and_step(db, actor)
    gate = await gates.open_approval(db, task, StepId(step.id), _items(3))
    await db.commit()
    spec = GateSpec.model_validate(gate.spec)
    assert spec.title == "Allow 3 actions?"
    assert spec.reason.count("Gmail: gmail_send_email") == 3, "one block per call, blank-line split"
    assert spec.reason.count("\n\n") == 2


async def test_store_answer_speaks_the_approval_channel(
    db: AsyncSession, actor: Actor, silent_dispatcher: object
) -> None:
    _, step = await _task_and_step(db, actor)
    step.resume_state = {"history": "h", "approval_calls": {"call_0": {"tool": "gmail_send_email"}}}
    await db.flush()

    await execution.store_answer(db, step.id, {"action": "accept", "content": {}})
    accepted = dict(step.resume_state or {})
    assert accepted["approval_action"] == "accept"
    assert accepted["cancelled"] is False
    assert "answer" not in accepted, "the ask channel stays untouched"

    step.resume_state = {"history": "h", "approval_calls": {"call_0": {}}}
    await execution.store_answer(db, step.id, {"action": "decline", "content": None})
    assert dict(step.resume_state or {})["approval_action"] == "decline"

    step.resume_state = {"history": "h", "approval_calls": {"call_0": {}}}
    await execution.store_answer(db, step.id, {"action": "cancel", "content": None})
    assert dict(step.resume_state or {})["cancelled"] is True


async def test_deny_on_timeout_is_structural_the_sweep_declines_an_expired_approve_gate(
    db: AsyncSession, actor: Actor, silent_dispatcher: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An approve gate has no questions, so the sweep's `on_timeout` read falls to
    proceed-with-defaults and the gate resolves DECLINE - which `store_answer` maps to a
    denial. Zero sweep changes; this pins that the structure holds."""
    # The TTL arm only: switched off, the kill-switch arm would decline other tests' gates too.
    monkeypatch.setattr(settings.computer_use, "enabled", True)
    task, step = await _task_and_step(db, actor)
    gate = await gates.open_approval(db, task, StepId(step.id), _items(1))
    gate.expires_at = utcnow() - dt.timedelta(minutes=1)
    await db.commit()

    swept = await gates.sweep(db)
    assert gate.id in swept
    await db.refresh(gate)
    assert gate.state == GateState.RESOLVED.value
    assert gate.resolution is not None and gate.resolution["action"] == "decline"


async def test_the_two_budgets_count_past_each_other(
    db: AsyncSession, actor: Actor, silent_dispatcher: object
) -> None:
    task, step = await _task_and_step(db, actor)
    await gates.open_approval(db, task, StepId(step.id), _items(1))
    await gates.open_approval(db, task, StepId(step.id), _items(1))
    await gates.open_question(db, task, StepId(step.id), question_set(("which one?", [])))
    await db.commit()

    assert await Gate.count_for_step(db, step.id) == 3
    assert await Gate.count_for_step(db, step.id, kind=GateKind.APPROVE.value) == 2
    assert await Gate.count_for_step(db, step.id, kind=GateKind.INPUT.value) == 1
