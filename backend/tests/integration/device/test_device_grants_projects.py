# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A device task and its Project: a device task never shares a
Project with members, a move and a new member meet under the project lock, a fork copies no folder. The
rig is `tests/support/rigs/device_grants_rig.py`."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from prismoid.api.app import create_app
from prismoid.conf import settings
from prismoid.db.models.actor import Actor
from prismoid.db.models.machine import Machine
from prismoid.db.models.project import Project, Role
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.task import Task
from prismoid.db.models.user import User
from prismoid.db.session import session_scope
from prismoid.errors import DeviceTaskPrivate
from prismoid.jobs import effects
from prismoid.operations import device_grants, steering
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.schemas.device import FolderGrant
from prismoid.schemas.machines import FrameDriverName, MachineState
from tests.support.cast import bound_scope, member
from tests.support.rigs.device_grants_rig import device_worker as device_worker
from tests.support.rigs.device_grants_rig import folder as folder
from tests.support.rigs.device_grants_rig import grants_grants, person
from tests.support.rigs.device_grants_rig import other_folder as other_folder

pytestmark = pytest.mark.integration


async def _project(s: Any, owner: Actor, *, with_member: Actor | None = None) -> Project:
    project = Project(name="Room", owner_actor_id=owner.id)
    s.add(project)
    await s.flush()
    if with_member is not None:
        await ProjectMember.grant(s, project.id, with_member.id, Role.EDITOR.value, added_by=owner.id)
    await s.commit()
    return project


async def test_device_task_into_project_with_members_is_device_task_private(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        bo = await person(s, "Bo")
        shared = await _project(s, ada, with_member=bo)
        with pytest.raises(DeviceTaskPrivate) as refused:
            await task_ops.create_task(s, ada, goal="in the room", project_id=shared.id)
        assert refused.value.code == "device_task_private"
        task = await task_ops.create_task(s, ada, goal="mine")
        with pytest.raises(DeviceTaskPrivate):
            await steering.move(s, ada, task.id, shared.id)
        assert (await s.get(Task, task.id)).project_id is None  # pyright: ignore[reportOptionalMemberAccess]


async def test_device_task_through_the_api_is_a_409(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        from prismoid.operations import auth as auth_ops

        made = await auth_ops.signup(
            s, email=f"e1{uuid.uuid4().hex[:8]}@example.test", name="E", password="a-long-enough-password"
        )
        owner = await s.get(Actor, made.user.actor_id)
        assert owner is not None
        bo = await person(s, "Bo")
        shared = await _project(s, owner, with_member=bo)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as http:
        h = {"Authorization": f"Bearer {made.token}"}
        r = await http.post("/v1/tasks", json={"goal": "g", "project_id": str(shared.id)}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "device_task_private"


async def test_owner_only_project_allowed(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        mine = await _project(s, ada)
        filed = await task_ops.create_task(s, ada, goal="in my folder", project_id=mine.id)
        loose = await task_ops.create_task(s, ada, goal="loose")
        await steering.move(s, ada, loose.id, mine.id)
        assert filed.project_id == mine.id and (await s.get(Task, loose.id)).project_id == mine.id  # pyright: ignore[reportOptionalMemberAccess]


async def test_add_member_to_project_holding_device_task_refused(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        bo = await person(s, "Bo")
        room = await _project(s, ada)
        await task_ops.create_task(s, ada, goal="on my computer", project_id=room.id)
        email = (await s.get(User, bo.id)).email  # pyright: ignore[reportOptionalMemberAccess]
        with pytest.raises(DeviceTaskPrivate):
            await project_ops.add_member(s, ada, room.id, email=email, role=Role.EDITOR)
        with pytest.raises(DeviceTaskPrivate):  # a transfer keeps the old owner on as a member
            await project_ops.transfer(s, ada, room.id, new_owner_email=email)
        assert await ProjectMember.for_project(s, room.id) == []

        monkeypatch.setattr(settings.worker, "frame_driver", "docker")  # a cloud Project takes members
        cloud = await _project(s, ada)
        await task_ops.create_task(s, ada, goal="in the cloud", project_id=cloud.id)
        added, _person_seen = await project_ops.add_member(s, ada, cloud.id, email=email, role=Role.EDITOR)
        assert added.actor_id == bo.id


async def test_add_member_waits_for_a_device_task_filed_meanwhile(silent_dispatcher: None) -> None:
    """The two sides lock the Project: a member added while a device task is being filed waits for
    its commit, then sees it and is refused."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        bo = await person(s, "Bo")
        room = await _project(s, ada)
        email = (await s.get(User, bo.id)).email  # pyright: ignore[reportOptionalMemberAccess]
        await s.commit()
    filing = asyncio.Event()
    keep = device_grants.keep_private

    async def slow_keep(session: Any, project: Project, **kw: Any) -> None:
        await keep(session, project, **kw)
        if kw.get("lock", True):
            filing.set()
        await asyncio.sleep(0.3)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(device_grants, "keep_private", slow_keep)

        async def file() -> Task:
            async with bound_scope(workspace.id) as s:
                return await task_ops.create_task(s, ada, goal="filed", project_id=room.id)

        async def add() -> Any:
            await asyncio.wait_for(filing.wait(), 10)
            async with bound_scope(workspace.id) as s:
                return await project_ops.add_member(s, ada, room.id, email=email, role=Role.EDITOR)

        filed, added = await asyncio.gather(file(), add(), return_exceptions=True)
    assert isinstance(filed, Task) and isinstance(added, DeviceTaskPrivate), (filed, added)


async def test_a_move_and_a_new_member_meet_under_the_project_lock(silent_dispatcher: None) -> None:
    """A device task moved into a Project while a member is being added: whichever commits first,
    the other is refused - never a device task in a Project with members."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        bo = await person(s, "Bo")
        room = await _project(s, ada)
        task = await task_ops.create_task(s, ada, goal="loose")
        email = (await s.get(User, bo.id)).email  # pyright: ignore[reportOptionalMemberAccess]
        ids = workspace.id, task.id, room.id
        await s.commit()
    moving = asyncio.Event()
    keep = device_grants.keep_private

    async def slow_keep(session: Any, project: Project, **kw: Any) -> None:
        await keep(session, project, **kw)
        moving.set()
        await asyncio.sleep(0.3)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(device_grants, "keep_private", slow_keep)

        async def move() -> str:
            async with bound_scope(ids[0]) as s:
                return await steering.move(s, ada, ids[1], ids[2])

        async def add() -> Any:
            await asyncio.wait_for(moving.wait(), 10)
            async with bound_scope(ids[0]) as s:
                return await project_ops.add_member(s, ada, ids[2], email=email, role=Role.EDITOR)

        moved, added = await asyncio.gather(move(), add(), return_exceptions=True)
    assert isinstance(moved, str) and isinstance(added, DeviceTaskPrivate), (moved, added)


async def test_fanout_off_for_device_task(silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from prismoid.operations import features

    async def always(*_a: Any, **_k: Any) -> bool:
        return True

    monkeypatch.setattr(features, "enabled", always)  # parallel fan-out on for everyone
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        on_device = await task_ops.create_task(s, ada, goal="device")
        in_cloud = await task_ops.create_task(s, ada, goal="cloud")
        in_cloud.machine_driver = FrameDriverName.DOCKER
        await s.commit()
    assert await effects.fanout_enabled(str(on_device.id)) is False
    assert await effects.fanout_enabled(str(in_cloud.id)) is True


async def test_fork_of_device_task_copies_no_folder(
    recording_dispatcher: Any, folder: Path, other_folder: Path
) -> None:
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        parent = await task_ops.create_task(s, ada, goal="fix it", folder=str(folder), docker=True)
        s.add(
            Machine(
                workspace_id=workspace.id,
                task_id=parent.id,
                driver="device",
                profile="base",
                state=MachineState.IDLE,
                frame_ref=f"device-{parent.id}",
                epoch=1,
                egress_hosts=[],
            )
        )
        parent.status = "delivered"
        await s.commit()
        child = await task_ops.fork(s, ada, parent.id)
        named = await task_ops.fork(s, ada, parent.id, folder=str(other_folder))
        assert await Machine.for_task(s, child.id) is None, "never seeded from the person's folder"
        assert await Machine.for_task(s, named.id) is None
        ids = workspace.id, parent.id, child.id, named.id
    workspace_id, parent_id, child_id, named_id = ids
    assert not [job for job, _key in recording_dispatcher.enqueues if job == "machine_seed"]
    assert await grants_grants(workspace_id, child_id) == ([], False), "no folder, no Docker: a fresh one"
    assert await grants_grants(workspace_id, named_id) == (
        [FolderGrant(path=str(other_folder), mode="rw", name="/work")],
        False,
    )
    assert await grants_grants(workspace_id, parent_id) == (
        [FolderGrant(path=str(folder), mode="rw", name="/work")],
        True,
    )
