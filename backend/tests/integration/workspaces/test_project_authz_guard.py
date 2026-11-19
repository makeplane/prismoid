# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Guard for the audit's #1 security finding: project-scoped MUTATIONS require editor+, not just
visibility.

`Task.get_for` is visibility only - every member of a project, VIEWER included, can fetch a project
task and read its stream. The bug the audit found: the consequential verbs took that
visibility as authority, so a viewer could `cancel`/`steer`/`pause`/`resume` a project task or
`resolve` its gates - actions the role matrix reserves for editor+. This pins the fix
(`operations.steering.require_project_editor` + the editor check in `operations.gates.resolve`):
a viewer is refused with `Forbidden`; an editor is allowed; a spaceless task (its creator's alone)
is untouched by this rule.

Lives in `tests/integration/` (not `tests/unit/` as the finding sketched): it drives the real
operations against a real session, so it needs the compose stack - and the conftest's cleanup is
integration-scoped. A DB test under `tests/unit/` would break the no-stack `pytest tests/unit`
gate that finding #4 exists to make meaningful.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.gate import Gate
from prismoid.db.models.project import Project
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.errors import Forbidden
from prismoid.operations import gates as gate_ops
from prismoid.operations import steering
from prismoid.operations import tasks as task_ops
from prismoid.schemas.plan import GateAction, GateKind, GateSpec, GateState
from tests.support.cast import member

pytestmark = pytest.mark.integration


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


async def _project_with_roles(db: AsyncSession) -> tuple[Project, Actor, Actor, Actor]:
    """A project with an owner, an editor member, and a viewer member - the role matrix, in rows."""
    owner, _ = await member(db, "Owner")
    editor, _ = await member(db, "Editor")
    viewer, _ = await member(db, "Viewer")
    await db.flush()
    project = Project(id=uuid.uuid4(), name="Team", owner_actor_id=owner.id)
    db.add(project)
    await db.flush()
    db.add_all(
        [
            ProjectMember(project_id=project.id, actor_id=editor.id, role="editor", added_by=owner.id),
            ProjectMember(project_id=project.id, actor_id=viewer.id, role="viewer", added_by=owner.id),
        ]
    )
    await db.flush()
    return project, owner, editor, viewer


async def _task(db: AsyncSession, owner: Actor, project: Project, status: str) -> Task:
    """A task filed in the project (created by the owner - an editor+), forced into `status`."""
    task = await task_ops.create_task(db, owner, goal="a project task", project_id=project.id)
    task.status = status  # set directly, as the fat-model tests do - we want a specific live state
    await db.commit()
    return task


async def _open_budget_gate(db: AsyncSession, task_id: uuid.UUID) -> Gate:
    gate = Gate(
        task_id=task_id,
        kind=GateKind.BUDGET.value,
        state=GateState.OPEN.value,
        spec=GateSpec(kind=GateKind.BUDGET, title="Budget reached", reason="raise it or cancel").model_dump(
            mode="json"
        ),
        expires_at=None,
    )
    db.add(gate)
    await db.flush()
    return gate


async def test_cancel_requires_editor_on_a_project_task(db: AsyncSession, silent_dispatcher: None) -> None:
    project, owner, editor, viewer = await _project_with_roles(db)
    viewers_task = await _task(db, owner, project, "running")
    with pytest.raises(Forbidden):
        await steering.cancel(db, viewer, viewers_task.id)
    editors_task = await _task(db, owner, project, "running")
    assert await steering.cancel(db, editor, editors_task.id) == "cancelled"


async def test_steer_requires_editor_on_a_project_task(db: AsyncSession, silent_dispatcher: None) -> None:
    project, owner, editor, viewer = await _project_with_roles(db)
    viewers_task = await _task(db, owner, project, "running")
    with pytest.raises(Forbidden):
        await steering.steer(db, viewer, viewers_task.id, "use euros")
    editors_task = await _task(db, owner, project, "running")
    await steering.steer(db, editor, editors_task.id, "use euros")  # must not raise


async def test_pause_requires_editor_on_a_project_task(db: AsyncSession, silent_dispatcher: None) -> None:
    project, owner, editor, viewer = await _project_with_roles(db)
    viewers_task = await _task(db, owner, project, "running")
    with pytest.raises(Forbidden):
        await steering.pause(db, viewer, viewers_task.id)
    editors_task = await _task(db, owner, project, "running")
    assert await steering.pause(db, editor, editors_task.id) == "paused"


async def test_resume_requires_editor_on_a_project_task(db: AsyncSession, silent_dispatcher: None) -> None:
    project, owner, editor, viewer = await _project_with_roles(db)
    viewers_task = await _task(db, owner, project, "paused")
    with pytest.raises(Forbidden):
        await steering.resume(db, viewer, viewers_task.id)
    editors_task = await _task(db, owner, project, "paused")
    assert await steering.resume(db, editor, editors_task.id) == "running"


async def test_gate_resolve_requires_editor_on_a_project_task(
    db: AsyncSession, silent_dispatcher: None
) -> None:
    project, owner, editor, viewer = await _project_with_roles(db)
    viewers_task = await _task(db, owner, project, "waiting")
    viewers_gate = await _open_budget_gate(db, viewers_task.id)
    with pytest.raises(Forbidden):
        await gate_ops.resolve(db, viewer, viewers_gate.id, GateAction.CANCEL)
    editors_task = await _task(db, owner, project, "waiting")
    editors_gate = await _open_budget_gate(db, editors_task.id)
    resolution = await gate_ops.resolve(db, editor, editors_gate.id, GateAction.CANCEL)
    assert resolution.action is GateAction.CANCEL


async def test_the_editor_check_leaves_a_spaceless_task_to_its_creator(
    db: AsyncSession, silent_dispatcher: None
) -> None:
    """A solo (spaceless) task is its creator's alone - the project-editor rule must not apply, and
    the creator can still steer/cancel their own work (the guard fences project tasks, nothing else)."""
    creator, _ = await member(db, "Solo")
    await db.flush()
    task = await task_ops.create_task(db, creator, goal="my own task")
    task.status = "running"
    await db.commit()
    await steering.steer(db, creator, task.id, "use euros")  # must not raise
    assert await steering.cancel(db, creator, task.id) == "cancelled"
