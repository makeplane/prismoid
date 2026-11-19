# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Write-time workspace stamping (ADR-061): roots take the bound workspace; ledger rows copy
their parent's; an unbound insert into a root fails closed."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from prismoid.db.base import utcnow
from prismoid.db.models import Actor, Project, Task, TaskEvent, UsageEvent, Workspace, WorkspaceKind
from prismoid.db.models.actor import ActorKind
from prismoid.db.session import session_scope
from prismoid.errors import TenantUnbound
from prismoid.operations import usage as usage_ops
from prismoid.schemas.ids import TaskId
from prismoid.schemas.model_class import Family
from prismoid.schemas.usage import Unit, UsageRecord

pytestmark = pytest.mark.integration


async def _workspace_and_actor() -> tuple[uuid.UUID, uuid.UUID]:
    async with session_scope() as s:
        actor = Actor(kind=ActorKind.HUMAN.value, display_name="Stamper", scopes=[], status="active")
        s.add(actor)
        await s.flush()
        workspace = await Workspace.create(s, name="Stamp Co", kind=WorkspaceKind.TEAM, created_by=actor.id)
        await s.commit()
        return workspace.id, actor.id


async def test_a_bound_insert_takes_the_workspace_and_ledger_rows_copy_their_task() -> None:
    workspace_id, actor_id = await _workspace_and_actor()
    task_id = uuid.uuid4()
    async with session_scope(workspace_id) as s:
        project = Project(name="Stamped", owner_actor_id=actor_id)
        s.add(project)
        s.add(Task(id=task_id, goal="g", creator_actor_id=actor_id, criteria={}, inputs=[]))
        await s.flush()
        assert project.workspace_id == workspace_id
        await usage_ops.record(
            s,
            UsageRecord(
                task_id=TaskId(task_id),
                provider="local",
                model="m",
                family=Family("qwen"),
                units={Unit.REQUEST: 1},
                cost_usd=Decimal("0.01"),
                price_version=1,
                at=utcnow(),
            ),
        )
        await s.commit()
    async with session_scope(platform=True) as s:
        assert await s.scalar(select(Task.workspace_id).where(Task.id == task_id)) == workspace_id
        assert (
            await s.scalar(select(UsageEvent.workspace_id).where(UsageEvent.task_id == task_id))
            == workspace_id
        )
        events = (await s.scalars(select(TaskEvent.workspace_id).where(TaskEvent.task_id == task_id))).all()
        assert events and set(events) == {workspace_id}


async def test_an_unbound_insert_into_a_tenant_root_fails_closed() -> None:
    """Strict: a root inserted with no workspace bound - or in a platform unit - never lands
    NULL; the flush raises before any SQL is sent."""
    _workspace, actor_id = await _workspace_and_actor()
    async with session_scope() as s:
        s.add(Project(name="Unbound", owner_actor_id=actor_id))
        with pytest.raises(TenantUnbound, match="insert into projects"):
            await s.flush()
    async with session_scope(platform=True) as s:
        s.add(Project(name="Platform", owner_actor_id=actor_id))
        with pytest.raises(TenantUnbound, match="insert into projects"):
            await s.flush()
