# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Machine's routes: who may press, what 202 says, what never leaks."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from prismoid.api.app import create_app
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.api_key import ApiKey
from prismoid.db.models.machine import Machine
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.task import Task
from prismoid.db.models.workspace import WorkspaceRole
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import auth as auth_ops
from prismoid.operations import machines
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.schemas.machines import MachineState
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def _person() -> tuple[str, uuid.UUID]:
    async with session_scope() as s:
        result = await auth_ops.signup(
            s, email=f"m-{uuid.uuid4().hex[:8]}@example.com", name="Ada", password="a-long-enough-password"
        )
        actor = await s.get(Actor, result.user.actor_id)
        assert actor is not None
        task = await task_ops.create_task(s, actor, goal="api")
        s.add(
            Machine(
                workspace_id=task.workspace_id,
                task_id=task.id,
                driver="docker",
                profile="base",
                state=MachineState.IDLE,
                state_since=utcnow(),
                last_active_at=utcnow(),
                epoch=1,
                frame_ref="machine-z-1",
            )
        )
        await s.commit()
        return result.token, task.id


@pytest.fixture
async def client(recording_dispatcher: Any) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        yield c


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_the_task_shows_its_machine_and_never_its_internals(client: AsyncClient) -> None:
    token, task_id = await _person()
    body = (await client.get(f"/v1/tasks/{task_id}", headers=_h(token))).json()
    assert body["machine"]["state"] == "idle" and body["machine"]["profile"] == "base"
    assert set(body["machine"]) == {
        "state",
        "profile",
        "state_since",
        "last_active_at",
        "checkpoint_at",
        "checkpoint_bytes",
        "browser_saved",
        "warm_seconds",
        "cost_usd",
        "control",
        "device_name",
        "driver",
    }
    assert body["machine"]["control"] is None
    assert body["machine"]["device_name"] is None, "a cloud Machine names no device"
    assert body["machine"]["driver"] is None, "a cloud Machine's driver is never named"


async def test_sleep_now_is_202_sleeping_and_a_stranger_gets_404(client: AsyncClient) -> None:
    token, task_id = await _person()
    stranger, _ = await _person()
    for verb in ("sleep", "reset"):
        r = await client.post(f"/v1/tasks/{task_id}/machine/{verb}", headers=_h(stranger))
        assert r.status_code == 404
    assert await _state_of(task_id) == "idle"
    r = await client.post(f"/v1/tasks/{task_id}/machine/sleep", headers=_h(token))
    assert r.status_code == 202 and r.json()["state"] == "sleeping"


async def test_a_running_machine_refuses_with_409(client: AsyncClient) -> None:
    token, task_id = await _person()
    async with session_scope() as s:
        await s.execute(update(Machine).where(Machine.task_id == task_id).values(state="running"))
        await s.commit()
    for verb in ("sleep", "reset"):
        r = await client.post(f"/v1/tasks/{task_id}/machine/{verb}", headers=_h(token))
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "conflict" and err["message"] == "It's working; pause the task first."


async def test_reset_is_202_gone(client: AsyncClient) -> None:
    token, task_id = await _person()
    r = await client.post(f"/v1/tasks/{task_id}/machine/reset", headers=_h(token))
    assert r.status_code == 202 and r.json()["state"] == "gone"


async def _shared() -> tuple[uuid.UUID, str, str, str]:
    """A Project task with a Machine, and the owner's, an editor's and a viewer's keys."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
        cy, _ = await member(s, "Cy", role=WorkspaceRole.MEMBER, workspace=workspace)
        project = (await project_ops.create(s, ada, name=f"m-{uuid.uuid4().hex[:6]}")).project
        s.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
        s.add(ProjectMember(project_id=project.id, actor_id=cy.id, role="viewer", added_by=ada.id))
        task = await task_ops.create_task(s, ada, goal="shared", project_id=project.id)
        s.add(
            Machine(
                workspace_id=workspace.id,
                task_id=task.id,
                driver="docker",
                profile="base",
                state=MachineState.IDLE,
                state_since=utcnow(),
                last_active_at=utcnow(),
                epoch=1,
                frame_ref="machine-y-1",
            )
        )
        keys = []
        for who in (ada, bob, cy):
            key, raw = ApiKey.issue(who.id, "t", workspace_id=workspace.id)
            s.add(key)
            keys.append(raw)
        await s.commit()
    return task.id, keys[0], keys[1], keys[2]


async def _state_of(task_id: uuid.UUID) -> str:
    async with row_scope(Task, task_id) as s:
        found = await machines.summary(s, task_id)
    assert found is not None
    return found.state.value


async def test_a_viewer_gets_403_and_the_machine_stays_while_an_editor_gets_202(client: AsyncClient) -> None:
    task_id, _, editor, viewer = await _shared()
    for verb in ("sleep", "reset"):
        r = await client.post(f"/v1/tasks/{task_id}/machine/{verb}", headers=_h(viewer))
        assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    assert await _state_of(task_id) == "idle"
    r = await client.post(f"/v1/tasks/{task_id}/machine/sleep", headers=_h(editor))
    assert r.status_code == 202 and r.json()["state"] == "sleeping"


async def _machineless() -> tuple[str, uuid.UUID]:
    async with session_scope() as s:
        result = await auth_ops.signup(
            s, email=f"n-{uuid.uuid4().hex[:8]}@example.com", name="Ada", password="a-long-enough-password"
        )
        actor = await s.get(Actor, result.user.actor_id)
        assert actor is not None
        task = await task_ops.create_task(s, actor, goal="bare")
        await s.commit()
        return result.token, task.id


async def test_a_task_with_no_machine_shows_null_and_the_buttons_say_no_machine(client: AsyncClient) -> None:
    token, task_id = await _machineless()
    body = (await client.get(f"/v1/tasks/{task_id}", headers=_h(token))).json()
    assert body["machine"] is None
    for verb in ("sleep", "reset"):
        r = await client.post(f"/v1/tasks/{task_id}/machine/{verb}", headers=_h(token))
        assert r.status_code == 404 and r.json()["error"]["code"] == "no_machine"
