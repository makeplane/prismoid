# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Archive - the pin's sibling: user-space triage that hides a settled task from default
lists (never a machine status, never an event) and comes back whole on unarchive. Same
curators as delete (creator or project owner), terminal-only for the same reason."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.errors import Conflict, Forbidden
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as ops
from prismoid.schemas.plan import TaskStatus
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def _mk_task(
    session: AsyncSession,
    actor: Actor,
    *,
    status: str = TaskStatus.DELIVERED.value,
    project_id: uuid.UUID | None = None,
) -> Task:
    task = await ops.create_task(session, actor, goal="archive probe", project_id=project_id)
    task.status = status  # direct write: the machine's path is not under test here
    await session.commit()
    return task


async def test_archive_hides_from_default_lists_and_unarchive_restores() -> None:
    async with session_scope() as session:
        me, _ = await member(session, "Me")
        await session.flush()
        task = await _mk_task(session, me)

        assert task.archived_at is None
        await ops.archive_task(session, me, task.id)
        assert task.archived_at is not None, "the stamp is set"

        active = await Task.list_for(session, me)
        assert task.id not in {t.id for t in active}, "default lists are the active world"
        drawer = await Task.list_for(session, me, archived=True)
        assert task.id in {t.id for t in drawer}, "the archive view holds it"
        assert (await Task.counts_for(session, me)).get(task.status, 0) == 0
        assert (await Task.counts_for(session, me, archived=True))[task.status] == 1

        stamped = task.archived_at
        await ops.archive_task(session, me, task.id)
        assert task.archived_at == stamped, "idempotent - the first stamp holds"

        await ops.unarchive_task(session, me, task.id)
        assert task.archived_at is None
        assert task.id in {t.id for t in await Task.list_for(session, me)}, "restored whole"


async def test_archive_refuses_live_work_and_mere_members_but_not_project_owners() -> None:
    async with session_scope() as session:
        owner, _ = await member(session, "Own")
        editor, _ = await member(session, "Edit")
        peer, _ = await member(session, "Peer")
        await session.flush()
        project = (await project_ops.create(session, owner, name="archive-room")).project
        session.add_all(
            [
                ProjectMember(project_id=project.id, actor_id=editor.id, role="editor", added_by=owner.id),
                ProjectMember(project_id=project.id, actor_id=peer.id, role="editor", added_by=owner.id),
            ]
        )
        await session.flush()

        running = await _mk_task(session, editor, status=TaskStatus.RUNNING.value, project_id=project.id)
        with pytest.raises(Conflict):
            await ops.archive_task(session, editor, running.id)

        done = await _mk_task(session, editor, project_id=project.id)
        with pytest.raises(Forbidden):
            # a fellow member SEES the task but is neither creator nor owner - delete's rule
            await ops.archive_task(session, peer, done.id)

        # the PROJECT OWNER may tidy their own room, even for a task they didn't create
        await ops.archive_task(session, owner, done.id)
        assert done.archived_at is not None
