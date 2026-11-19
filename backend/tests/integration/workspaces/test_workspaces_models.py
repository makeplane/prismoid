# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The four workspace tables."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models import (
    Actor,
    Project,
    Task,
    Workspace,
    WorkspaceInvite,
    WorkspaceKind,
    WorkspaceMember,
    WorkspaceRole,
    tenant,
)
from prismoid.db.models.actor import ActorKind
from prismoid.db.session import session_scope
from tests.support.cast import a_workspace, member

pytestmark = pytest.mark.integration


async def _human(session: AsyncSession, name: str) -> Actor:
    actor = Actor(kind=ActorKind.HUMAN.value, display_name=name, scopes=[], status="active")
    session.add(actor)
    await session.flush()
    return actor


async def test_workspace_membership_and_roster() -> None:
    async with session_scope() as s:
        ada = await _human(s, "Ada")
        workspace = await Workspace.create(s, name="Acme Labs", kind=WorkspaceKind.TEAM, created_by=ada.id)
        await WorkspaceMember.add(s, workspace.id, ada.id, WorkspaceRole.OWNER)
        await s.commit()
        assert workspace.slug.startswith("acme-labs-")
        member = await WorkspaceMember.get(s, workspace.id, ada.id)
        assert member is not None and member.role == WorkspaceRole.OWNER
        assert await WorkspaceMember.owner_count(s, workspace.id) == 1
        pairs = await Workspace.for_actor(s, ada.id)
        assert [o.id for o, _ in pairs] == [workspace.id]


async def test_invite_code_is_single_live_and_hashed() -> None:
    async with session_scope() as s:
        ada = await _human(s, "Ada")
        workspace = await Workspace.create(s, name="Acme", kind=WorkspaceKind.TEAM, created_by=ada.id)
        _first, raw1 = await WorkspaceInvite.mint(
            s, workspace.id, "Bob@Example.com", WorkspaceRole.MEMBER, ada.id
        )
        second, raw2 = await WorkspaceInvite.mint(
            s, workspace.id, "bob@example.com", WorkspaceRole.MEMBER, ada.id
        )
        await s.commit()
        assert raw2.startswith("oi_") and raw2 not in second.code_hash
        assert await WorkspaceInvite.by_code(s, raw1) is None  # superseded
        live = await WorkspaceInvite.by_code(s, raw2)
        assert live is not None and live.email == "bob@example.com"
        second.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        await s.commit()
        assert await WorkspaceInvite.by_code(s, raw2) is None  # expired


async def test_a_task_cannot_file_into_another_workspaces_project() -> None:
    """The composite (project_id, workspace_id) reference makes a cross-workspace filing impossible
    at the database, not only refused in operations/."""
    async with session_scope() as s:
        ada, _workspace_a = await member(s, "Ada")
        room = Project(name="A's room", owner_actor_id=ada.id)
        s.add(room)
        await s.flush()
        bob, workspace_b = await a_workspace(s, "Bob")
        tenant.bind(s, workspace_b.id)
        s.add(
            Task(
                id=uuid.uuid4(), goal="g", creator_actor_id=bob.id, criteria={}, inputs=[], project_id=room.id
            )
        )
        with pytest.raises(IntegrityError, match="fk_tasks_project_workspace"):
            await s.flush()


async def test_deleting_a_project_nulls_its_tasks_project_and_keeps_their_workspace() -> None:
    """ON DELETE SET NULL (project_id): the room goes, the task stays filed in its workspace."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        room = Project(name="Doomed", owner_actor_id=ada.id)
        s.add(room)
        await s.flush()
        task_id = uuid.uuid4()
        s.add(Task(id=task_id, goal="g", creator_actor_id=ada.id, criteria={}, inputs=[], project_id=room.id))
        await s.commit()
        await s.execute(delete(Project).where(Project.id == room.id))
        await s.commit()
        row = (await s.execute(select(Task.project_id, Task.workspace_id).where(Task.id == task_id))).one()
        assert row.project_id is None and row.workspace_id == workspace.id
