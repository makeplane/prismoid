# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The steer door over HTTP (ADR-064): 202 with the command, a 409 `stale_steer` that names
what was missed, retract by the author only, and the epoch on the task's detail."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from prismoid.api.app import create_app
from prismoid.db.models.api_key import ApiKey
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.workspace import WorkspaceRole
from prismoid.db.session import session_scope
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.schemas.plan import TaskStatus
from tests.support.cast import member

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(recording_dispatcher: Any) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        yield c


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _shared() -> tuple[uuid.UUID, str, str, str]:
    """A running Project task, and the owner's, an editor's and a viewer's keys."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
        cy, _ = await member(s, "Cy", role=WorkspaceRole.MEMBER, workspace=workspace)
        project = (await project_ops.create(s, ada, name=f"s-{uuid.uuid4().hex[:6]}")).project
        s.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
        s.add(ProjectMember(project_id=project.id, actor_id=cy.id, role="viewer", added_by=ada.id))
        task = await task_ops.create_task(s, ada, goal="shared", project_id=project.id)
        task.transition(TaskStatus.PLANNING)
        task.transition(TaskStatus.RUNNING)
        keys = []
        for who in (ada, bob, cy):
            key, raw = ApiKey.issue(who.id, "t", workspace_id=workspace.id)
            s.add(key)
            keys.append(raw)
        await s.commit()
    return task.id, keys[0], keys[1], keys[2]


async def test_steer_is_202_with_its_command_and_a_stale_one_is_409_with_missed(client: AsyncClient) -> None:
    task_id, ada, bob, _cy = await _shared()
    assert (await client.get(f"/v1/tasks/{task_id}", headers=_h(ada))).json()["plan_epoch"] == 1
    cid = str(uuid.uuid4())
    r = await client.post(
        f"/v1/tasks/{task_id}/steer",
        headers=_h(bob),
        json={"directive": "use euros", "command_id": cid, "issued_epoch": 1},
    )
    assert r.status_code == 202 and r.json()["command_id"] == cid and r.json()["epoch"] == 2
    assert r.json()["state"] == "accepted" and r.json()["steered"] == "use euros"
    # Ada typed from epoch 1 too and missed Bob's (his own second steer would not be stale - R51)
    r = await client.post(
        f"/v1/tasks/{task_id}/steer", headers=_h(ada), json={"directive": "use pounds", "issued_epoch": 1}
    )
    body = r.json()["error"]
    assert r.status_code == 409 and body["code"] == "stale_steer"
    assert body["missed"][0]["author"] == "Bob" and body["missed"][0]["text"] == "use euros"
    assert (await client.get(f"/v1/tasks/{task_id}", headers=_h(ada))).json()["plan_epoch"] == 2


async def test_a_resend_is_the_same_answer(client: AsyncClient) -> None:
    task_id, _ada, bob, _cy = await _shared()
    body = {"directive": "use euros", "command_id": str(uuid.uuid4()), "issued_epoch": 1}
    first = await client.post(f"/v1/tasks/{task_id}/steer", headers=_h(bob), json=body)
    again = await client.post(f"/v1/tasks/{task_id}/steer", headers=_h(bob), json=body)
    assert first.status_code == again.status_code == 202 and first.json() == again.json()


async def test_bad_input_is_422(client: AsyncClient) -> None:
    task_id, _ada, bob, _cy = await _shared()
    for body in ({"directive": "   "}, {"directive": "x", "issued_epoch": 99}, {"directive": "x" * 2001}):
        r = await client.post(f"/v1/tasks/{task_id}/steer", headers=_h(bob), json=body)
        assert r.status_code == 422 and r.json()["error"]["code"] == "validation_failed", body


async def test_retract_is_the_authors_and_a_viewer_is_403(client: AsyncClient) -> None:
    task_id, ada, bob, cy = await _shared()
    r = await client.post(f"/v1/tasks/{task_id}/steer", headers=_h(bob), json={"directive": "x"})
    cid = r.json()["command_id"]
    assert (await client.post(f"/v1/tasks/{task_id}/steers/{cid}/retract", headers=_h(cy))).status_code == 403
    assert (
        await client.post(f"/v1/tasks/{task_id}/steers/{cid}/retract", headers=_h(ada))
    ).status_code == 403
    r = await client.post(f"/v1/tasks/{task_id}/steers/{cid}/retract", headers=_h(bob))
    assert r.status_code == 200 and r.json()["state"] == "retracted"
    again = await client.post(f"/v1/tasks/{task_id}/steers/{cid}/retract", headers=_h(bob))
    assert again.status_code == 409 and again.json()["error"]["code"] == "conflict"
    nothing = await client.post(f"/v1/tasks/{task_id}/steers/{uuid.uuid4()}/retract", headers=_h(bob))
    assert nothing.status_code == 404


async def test_replan_still_takes_a_directive(client: AsyncClient) -> None:
    task_id, _ada, bob, _cy = await _shared()
    r = await client.post(f"/v1/tasks/{task_id}/replan", headers=_h(bob), json={"directive": ""})
    assert r.status_code == 422
