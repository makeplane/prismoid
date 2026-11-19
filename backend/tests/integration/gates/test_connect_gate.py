# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The connect gate (ADR-054): the in-conversation connect block's machinery.

The laws: `open_connect` builds a CONNECT gate carrying slug + why (and knocks the inbox);
`resolve` on ACCEPT attaches the CREATOR's now-live connector to the task before the wake
(false, honestly, for a stranger, a dead row, or a full task); `_park_on_connect` resolves the
agent's service word to a catalog entry or the owner's own row; `store_answer` speaks the
connect channel; the TTL declines structurally (questions=None, the approve gate's law).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

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
from prismoid.operations import connectors as connector_ops
from prismoid.operations import execution, gates
from prismoid.operations import tasks as task_ops
from prismoid.operations.execution import ConnectDeferral
from prismoid.operations.execution import parks as ex_parks
from prismoid.schemas.connectors import ConnectorStatus, ConnectorToolInfo, ProbeResult
from prismoid.schemas.ids import StepId
from prismoid.schemas.plan import GateAction, GateKind, GateSpec, GateState
from tests.support.cast import member

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("silent_dispatcher")]

MCP = "https://mcp.example.test/mcp"


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


@pytest.fixture
async def actor(db: AsyncSession) -> Actor:
    row, _ = await member(db, "Connector Person")
    await db.commit()
    return row


@pytest.fixture
def live_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    async def probe(url: str, **kw: Any) -> ProbeResult:
        return ProbeResult(tools=(ConnectorToolInfo(name="search", read_only=True),))

    monkeypatch.setattr(connector_ops, "probe_server", probe)


async def _task_and_step(db: AsyncSession, actor: Actor, goal: str = "make a page") -> tuple[Task, PlanStep]:
    task = await task_ops.create_task(db, actor, goal=goal)
    row = await db.get(Task, task.id)
    assert row is not None
    step = PlanStep(task_id=row.id, ordinal=0, title="do the thing")
    db.add(step)
    await db.flush()
    return row, step


async def test_open_connect_builds_the_block_the_knock_and_the_event(db: AsyncSession, actor: Actor) -> None:
    task, step = await _task_and_step(db, actor)
    gate = await gates.open_connect(
        db, task, StepId(step.id), slug="notion", display="Notion", why="To create the page there."
    )
    await db.commit()

    spec = GateSpec.model_validate(gate.spec)
    assert spec.kind is GateKind.CONNECT and spec.questions is None
    assert spec.title == "Connect Notion to continue?"
    assert spec.connector_slug == "notion"
    assert spec.reason == "To create the page there."
    assert gate.expires_at is not None and gate.expires_at > utcnow() + dt.timedelta(hours=23)

    knock = await db.scalar(select(InboxItem).where(InboxItem.gate_id == gate.id))
    assert knock is not None and knock.kind == InboxKind.GATE_OPENED.value
    assert knock.title == "Connect Notion to continue"

    from prismoid.schemas.events import GateOpened

    opened = [e for e in await TaskEvent.replay(db, task.id) if isinstance(e, GateOpened)]
    assert opened and opened[-1].kind is GateKind.CONNECT
    assert opened[-1].connector_slug == "notion", "the stream tells the block what to offer"


async def test_accept_attaches_the_creators_live_connector_before_the_wake(
    db: AsyncSession, actor: Actor, live_probe: None
) -> None:
    row = await connector_ops.create_connector(db, actor, server_url=MCP, name="Notion")
    assert row.status == ConnectorStatus.LIVE
    task, step = await _task_and_step(db, actor)
    assert task.connector_ids == []
    gate = await gates.open_connect(db, task, StepId(step.id), slug="notion", display="Notion", why="why")
    await db.commit()

    resolution = await gates.resolve(db, actor, gate.id, GateAction.ACCEPT)
    assert resolution.content == {"connected": True}, "the receipt rides the resolution"
    await db.refresh(task)
    assert task.connector_ids == [str(row.id)], "the resumed attempt will mount its tools"

    # Idempotent: a second accept (the already-resolved path returns the prior resolution).
    again = await gates.resolve(db, actor, gate.id, GateAction.ACCEPT)
    assert again.content == {"connected": True}


async def test_accept_is_honest_when_it_cannot_attach(
    db: AsyncSession, actor: Actor, live_probe: None
) -> None:
    task, step = await _task_and_step(db, actor)
    # No such connector for this creator: accept resumes, tool-less, and says so.
    gate = await gates.open_connect(db, task, StepId(step.id), slug="ghost", display="Ghost", why="w")
    await db.commit()
    resolution = await gates.resolve(db, actor, gate.id, GateAction.ACCEPT)
    assert resolution.content == {"connected": False}
    await db.refresh(task)
    assert task.connector_ids == []

    # A non-live row: same honest no.
    parked = await connector_ops.create_connector(db, actor, server_url=MCP + "2", name="Linear")
    parked.status = ConnectorStatus.NEEDS_REAUTH
    await db.commit()
    gate2 = await gates.open_connect(db, task, StepId(step.id), slug="linear", display="Linear", why="w")
    await db.commit()
    resolution2 = await gates.resolve(db, actor, gate2.id, GateAction.ACCEPT)
    assert resolution2.content == {"connected": False}


async def test_store_answer_speaks_the_connect_channel(db: AsyncSession, actor: Actor) -> None:
    _, step = await _task_and_step(db, actor)
    base = {"history": "h", "tool_call_id": "c1", "connect": {"slug": "notion", "display": "Notion"}}

    step.resume_state = dict(base)
    await db.flush()
    await execution.store_answer(db, step.id, {"action": "accept", "content": {"connected": True}})
    assert dict(step.resume_state or {})["connect_result"] == "connected"

    step.resume_state = dict(base)
    await execution.store_answer(db, step.id, {"action": "accept", "content": {"connected": False}})
    assert dict(step.resume_state or {})["connect_result"] == "accepted_unattached"

    step.resume_state = dict(base)
    await execution.store_answer(db, step.id, {"action": "decline", "content": None})
    assert dict(step.resume_state or {})["connect_result"] == "declined"
    assert "answer" not in dict(step.resume_state or {}), "the ask channel stays untouched"


async def test_park_resolves_the_service_word_to_catalog_row_or_honestly_nothing(
    db: AsyncSession, actor: Actor, live_probe: None
) -> None:
    task, step = await _task_and_step(db, actor)
    task.status = "running"  # a park happens mid-run; running → waiting is the legal edge
    await db.flush()

    async def park(service: str) -> GateSpec:
        deferral = ConnectDeferral(tool_call_id="c1", service=service, why="w")
        gate_id = await ex_parks.park_on_connect(db, task, step, deferral)
        gate = await db.get(Gate, gate_id)
        assert gate is not None
        return GateSpec.model_validate(gate.spec)

    # A catalog slug wears the catalog's display name.
    spec = await park("notion")
    assert (spec.connector_slug, spec.title) == ("notion", "Connect Notion to continue?")

    # The owner's own custom row, found by slug, wears the row's name.
    await connector_ops.create_connector(db, actor, server_url=MCP, name="My CRM")
    spec2 = await park("my_crm")
    assert spec2.connector_slug == "my_crm" and "My CRM" in spec2.title

    # Nothing matches: the gate still opens, slug empty - the block points at the page.
    spec3 = await park("teleporter")
    assert spec3.connector_slug == "" and "teleporter" in spec3.title


async def test_the_ttl_declines_a_connect_gate_and_budgets_count_by_kind(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The TTL arm only: switched off, the kill-switch arm would decline other tests' gates too.
    monkeypatch.setattr(settings.computer_use, "enabled", True)
    task, step = await _task_and_step(db, actor)
    gate = await gates.open_connect(db, task, StepId(step.id), slug="notion", display="Notion", why="w")
    gate.expires_at = utcnow() - dt.timedelta(minutes=1)
    await db.commit()

    swept = await gates.sweep(db)
    assert gate.id in swept
    await db.refresh(gate)
    assert gate.state == GateState.RESOLVED.value
    assert gate.resolution is not None and gate.resolution["action"] == "decline"

    assert await Gate.count_for_step(db, step.id, kind=GateKind.CONNECT.value) == 1
    assert await Gate.count_for_step(db, step.id, kind=GateKind.INPUT.value) == 0
