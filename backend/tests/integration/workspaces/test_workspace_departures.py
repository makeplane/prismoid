# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Leaving or losing a workspace stops your work there.

A person removed from a workspace, or leaving it, keeps no running, queued or parked task in it: each is
cancelled through the one cancel machinery, gates resolved as cancel. Their tasks in other workspaces are
untouched, and a cancel that fails keeps the membership.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.gate import Gate
from prismoid.db.models.task import Task
from prismoid.db.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from prismoid.db.session import session_scope
from prismoid.errors import Conflict, Forbidden
from prismoid.operations import departures
from prismoid.operations import tasks as task_ops
from prismoid.schemas.plan import GateKind, GateSpec, GateState, TaskStatus
from tests.support.cast import member

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("silent_dispatcher")]


async def _running(db: AsyncSession, who: Actor, workspace: Workspace, *, parked: bool = False) -> uuid.UUID:
    tenant.bind(db, workspace.id, WorkspaceRole.MEMBER.value)
    task = await task_ops.create_task(db, who, goal="work that must stop when I go")
    row = await db.get(Task, task.id)
    assert row is not None
    row.transition(TaskStatus.PLANNING)
    row.transition(TaskStatus.RUNNING)
    if parked:
        row.transition(TaskStatus.WAITING)
        db.add(
            Gate(
                task_id=task.id,
                kind=GateKind.BUDGET.value,
                state=GateState.OPEN.value,
                spec=GateSpec(kind=GateKind.BUDGET, title="Budget reached", reason="spent").model_dump(
                    mode="json"
                ),
                expires_at=None,
            )
        )
    await db.commit()
    return task.id


async def _cast(
    db: AsyncSession,
) -> tuple[Actor, Actor, Workspace, Workspace, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Ann owns the workspace; Ben is a member there and owns his own; he has a task in each."""
    ann, ann_ws = await member(db, "Ann")
    ben, ben_ws = await _fresh(db, "Ben")
    await WorkspaceMember.add(db, ann_ws.id, ben.id, WorkspaceRole.MEMBER)
    await db.commit()
    here = await _running(db, ben, ann_ws, parked=True)
    there = await _running(db, ben, ben_ws)
    return ann, ben, ann_ws, ben_ws, here, there, ann_ws.id


async def _fresh(db: AsyncSession, name: str) -> tuple[Actor, Workspace]:
    """A person with a workspace of their own, whatever the session is bound to."""
    async with session_scope() as other:
        actor, workspace = await member(other, name)
        await other.commit()
        actor_id, workspace_id = actor.id, workspace.id
    got = await db.get(Actor, actor_id)
    ws = await db.get(Workspace, workspace_id)
    assert got is not None and ws is not None
    return got, ws


async def _status(db: AsyncSession, task_id: uuid.UUID) -> str:
    db.expire_all()
    row = await db.get(Task, task_id)
    assert row is not None
    return row.status


async def test_removing_a_member_cancels_their_running_tasks_in_that_workspace_only(db: AsyncSession) -> None:
    ann, ben, ann_ws, _, here, there, ws_id = await _cast(db)
    ben_id = ben.id
    tenant.bind(db, ann_ws.id, WorkspaceRole.OWNER.value)

    await departures.remove_member(db, ann, ws_id, ben_id)

    assert await _status(db, here) == TaskStatus.CANCELLED.value
    assert await _status(db, there) == TaskStatus.RUNNING.value
    assert await WorkspaceMember.get(db, ws_id, ben_id) is None
    assert await Gate.open_for_task(db, here) == []


async def test_leaving_a_workspace_cancels_your_tasks_there(db: AsyncSession) -> None:
    _, ben, ann_ws, _, here, there, ws_id = await _cast(db)
    ben_id = ben.id
    tenant.bind(db, ann_ws.id, WorkspaceRole.MEMBER.value)

    await departures.leave_workspace(db, ben, ws_id)

    assert await _status(db, here) == TaskStatus.CANCELLED.value
    assert await _status(db, there) == TaskStatus.RUNNING.value
    assert await WorkspaceMember.get(db, ws_id, ben_id) is None


async def test_removing_a_member_cancels_a_teammates_task_running_their_turn(db: AsyncSession) -> None:
    """Ann's task whose current turn is Ben's follow-up spends for Ben: removing him stops it too, and
    Ann's own turn on her other task runs on."""
    ann, ben, ann_ws, _, _, _, ws_id = await _cast(db)
    ann_id, ben_id = ann.id, ben.id
    his_turn = await _running(db, ann, ann_ws)
    her_turn = await _running(db, ann, ann_ws)
    row = await db.get(Task, his_turn)
    assert row is not None
    row.turn_actor_id = ben_id
    await db.commit()
    tenant.bind(db, ws_id, WorkspaceRole.OWNER.value)

    await departures.remove_member(db, ann, ws_id, ben_id)

    assert await _status(db, his_turn) == TaskStatus.CANCELLED.value
    assert await _status(db, her_turn) == TaskStatus.RUNNING.value
    assert await WorkspaceMember.get(db, ws_id, ann_id) is not None


async def test_a_cancel_that_fails_keeps_the_membership(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cancel comes first and the removal last, so a failure between them leaves the person a member
    (a retry finishes the job); the other order would leave someone out with their work still spending."""
    ann, ben, ann_ws, _, here, _, ws_id = await _cast(db)
    ben_id = ben.id
    tenant.bind(db, ann_ws.id, WorkspaceRole.OWNER.value)

    async def boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("cancel failed")

    monkeypatch.setattr(departures.steering, "cancel_task_row", boom)
    with pytest.raises(RuntimeError):
        await departures.remove_member(db, ann, ws_id, ben_id)

    await db.rollback()
    assert await WorkspaceMember.get(db, ws_id, ben_id) is not None
    assert await _status(db, here) != TaskStatus.CANCELLED.value


async def _team(db: AsyncSession, *roles: WorkspaceRole) -> tuple[Workspace, list[Actor]]:
    """A workspace founded by an owner, plus one more person per role given."""
    first, workspace = await _fresh(db, "Founder")
    people = [first]
    for role in roles:
        who, _ = await _fresh(db, f"Person {len(people)}")
        await WorkspaceMember.add(db, workspace.id, who.id, role)
        people.append(who)
    await db.commit()
    return workspace, people


async def _nothing_happened(
    db: AsyncSession, task_id: uuid.UUID, workspace_id: uuid.UUID, who: uuid.UUID
) -> None:
    assert await _status(db, task_id) == TaskStatus.RUNNING.value
    assert await WorkspaceMember.get(db, workspace_id, who) is not None


async def test_a_refused_removal_by_a_non_owner_cancels_nothing(db: AsyncSession) -> None:
    """The refusal comes before the cancel: an admin may not remove an owner, and the owner's work runs on."""
    workspace, (owner, _, admin) = await _team(db, WorkspaceRole.OWNER, WorkspaceRole.ADMIN)
    owner_id, workspace_id = owner.id, workspace.id
    task = await _running(db, owner, workspace)
    tenant.bind(db, workspace_id, WorkspaceRole.ADMIN.value)

    with pytest.raises(Forbidden):
        await departures.remove_member(db, admin, workspace_id, owner_id)

    await db.rollback()
    await _nothing_happened(db, task, workspace_id, owner_id)


async def test_a_refused_removal_of_the_last_owner_cancels_nothing(db: AsyncSession) -> None:
    workspace, (owner,) = await _team(db)
    owner_id, workspace_id = owner.id, workspace.id
    task = await _running(db, owner, workspace)
    tenant.bind(db, workspace_id, WorkspaceRole.OWNER.value)

    with pytest.raises(Conflict):
        await departures.remove_member(db, owner, workspace_id, owner_id)

    await db.rollback()
    await _nothing_happened(db, task, workspace_id, owner_id)


async def test_a_refused_leave_of_your_only_workspace_cancels_nothing(db: AsyncSession) -> None:
    """Two owners, so the last-owner guard passes and it is the only-workspace rule that refuses."""
    workspace, (owner, _) = await _team(db, WorkspaceRole.OWNER)
    owner_id, workspace_id = owner.id, workspace.id
    task = await _running(db, owner, workspace)
    tenant.bind(db, workspace_id, WorkspaceRole.OWNER.value)

    with pytest.raises(Conflict, match="only workspace"):
        await departures.leave_workspace(db, owner, workspace_id)

    await db.rollback()
    await _nothing_happened(db, task, workspace_id, owner_id)
