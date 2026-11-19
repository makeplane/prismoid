# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The grants API and a worktree's grants: list, add, revoke,
a revoked domain that asks again, and a worktree carrying its git folders. The rig is
`tests/support/rigs/device_grants_rig.py`."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from prismoid.api.app import create_app
from prismoid.db.models.device_grant import DeviceGrant
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.device import exec as device_exec
from prismoid.device.frames import FrameRegistry, GrantRefused, worktree_git
from prismoid.operations import device_grants, execution, machines
from prismoid.operations import egress_allowances as allow_ops
from prismoid.operations import tasks as task_ops
from prismoid.schemas.contract import Contract
from prismoid.schemas.device import FolderGrant, FrameSpecIn, GrantsIn
from prismoid.schemas.device_grants import GrantKind, GrantMode
from prismoid.schemas.machines import FrameKind
from tests.support.cast import bound_scope, member
from tests.support.rigs.device_grants_rig import device_worker as device_worker
from tests.support.rigs.device_grants_rig import folder as folder
from tests.support.rigs.device_grants_rig import grants_grants, worktree
from tests.support.rigs.device_grants_rig import other_folder as other_folder

pytestmark = pytest.mark.integration


async def test_grants_list_add_revoke_api(silent_dispatcher: None, folder: Path, other_folder: Path) -> None:
    from prismoid.operations import auth as auth_ops

    async with session_scope() as s:
        ana = await auth_ops.signup(
            s, email=f"ana{uuid.uuid4().hex[:8]}@example.test", name="Ana", password="a-long-enough-password"
        )
        bo = await auth_ops.signup(
            s, email=f"bo{uuid.uuid4().hex[:8]}@example.test", name="Bo", password="a-long-enough-password"
        )
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as http:
        h = {"Authorization": f"Bearer {ana.token}"}
        made = await http.post("/v1/tasks", json={"goal": "fix it", "folder": str(folder)}, headers=h)
        assert made.status_code == 202, made.text
        task_id = made.json()["task_id"]
        listed = (await http.get("/v1/device/grants", headers=h)).json()
        assert [(g["kind"], g["path"], g["mode"]) for g in listed] == [("folder", str(folder), "rw")]

        ro = await http.post(
            "/v1/device/grants",
            json={"task_id": task_id, "kind": "folder", "path": str(other_folder), "mode": "ro"},
            headers=h,
        )
        assert ro.status_code == 201 and ro.json()["mode"] == "ro", ro.text
        docker = await http.post("/v1/device/grants", json={"task_id": task_id, "kind": "docker"}, headers=h)
        again = await http.post("/v1/device/grants", json={"task_id": task_id, "kind": "docker"}, headers=h)
        assert docker.status_code == again.status_code == 201 and docker.json()["id"] == again.json()["id"]
        bad = await http.post(
            "/v1/device/grants", json={"task_id": task_id, "kind": "folder", "path": "/etc"}, headers=h
        )
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "folder_refused"
        busy = await http.post("/v1/tasks", json={"goal": "twin", "folder": str(folder)}, headers=h)
        assert busy.status_code == 409 and busy.json()["error"]["code"] == "folder_busy"
        assert task_id in busy.json()["error"]["message"]

        mine = (await http.get("/v1/device/grants", params={"task_id": task_id}, headers=h)).json()
        assert [g["kind"] for g in mine] == ["folder", "folder", "docker"]
        theirs = await http.get("/v1/device/grants", headers={"Authorization": f"Bearer {bo.token}"})
        assert theirs.json() == []
        stranger = await http.delete(
            f"/v1/device/grants/{ro.json()['id']}", headers={"Authorization": f"Bearer {bo.token}"}
        )
        assert stranger.status_code == 404

        gone = await http.delete(f"/v1/device/grants/{ro.json()['id']}", headers=h)
        assert gone.status_code == 200 and gone.json() == {"ok": True}
        left = (await http.get("/v1/device/grants", headers=h)).json()
        assert [(g["kind"], g["mode"]) for g in left] == [("folder", "rw"), ("docker", None)]
    async with session_scope() as s:
        row = await s.get(DeviceGrant, uuid.UUID(ro.json()["id"]))
        assert row is not None and row.revoked_at is not None, "revoke is soft: the history stays"


async def test_revoked_domain_asks_again(
    fake_frames: Any, silent_dispatcher: None, folder: Path, other_folder: Path
) -> None:
    """Postgres is the authority, and the helper is told: an always-allowed site revoked is out of
    the next step's list (so the browser asks again), and a folder revoked is out of the grants the
    frame is told at its next bind."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="fix it", folder=str(folder))
        ro = await device_grants.add_grant(
            s, ada, task.id, kind=GrantKind.FOLDER, path=str(other_folder), mode=GrantMode.RO
        )
        site = await allow_ops.add_allowance(s, ada, "status.example.com")
        step = PlanStep(
            id=uuid.uuid4(), task_id=task.id, ordinal=0, kind="step", state="running", title="w", spec={}
        )
        s.add(step)
        await s.commit()

    async def step_list() -> set[str]:
        async with bound_scope(workspace.id) as s:
            row = await s.get(Task, task.id)
            assert row is not None
            return await execution._browser_allowlist(s, row, Contract(goal="g"))  # pyright: ignore[reportPrivateUsage]

    async def acquire(attempt: int) -> Any:
        allowed = await step_list()
        async with bound_scope(workspace.id) as s:
            row, held = await s.get(Task, task.id), await s.get(PlanStep, step.id)
            assert row is not None and held is not None
            held.attempt = attempt
            return await machines.acquire(s, row, held, profile="base", allow_domains=sorted(allowed))

    first = await acquire(1)
    live = fake_frames.world[first.frame.ref]
    assert "status.example.com" in live.egress_hosts
    assert live.granted == (
        [
            FolderGrant(path=str(folder), mode="rw", name="/work"),
            FolderGrant(path=str(other_folder), mode="ro", name=ro.name),
        ],
        False,
    )
    await machines.release(first, parked=False)

    async with bound_scope(workspace.id) as s:
        await device_grants.revoke_grant(s, ada, ro.id)
        await allow_ops.revoke_allowance(s, ada, site.id)
    assert "status.example.com" not in await step_list(), "revoked: the browser asks again"
    again = await acquire(2)
    assert again.frame.ref == first.frame.ref and not again.woke, "the same frame, bound warm"
    live = fake_frames.world[again.frame.ref]
    assert live.granted == ([FolderGrant(path=str(folder), mode="rw", name="/work")], False)
    assert "status.example.com" not in live.egress_hosts
    await machines.release(again, parked=False)


def test_a_worktree_grant_carries_its_git_folders_and_the_helper_checks_them(
    tmp_path: Path, folder: Path
) -> None:
    main, wt = worktree(folder)
    git = worktree_git(str(wt))
    assert git == [str((main / ".git" / "worktrees" / "wt").resolve()), str((main / ".git").resolve())]
    assert worktree_git(str(main)) == [], "a plain repository needs nothing beyond itself"

    registry = FrameRegistry(tmp_path / "home")
    spec = FrameSpecIn(
        task_id="t1", kind=FrameKind.MACHINE, grants=[FolderGrant(path=str(wt), mode="rw", git=git)]
    )
    frame = registry.create(spec)
    assert frame.linked_git == tuple(git) and frame.paths.work == str(wt.resolve())
    fence = device_exec.fence_for(frame, system=False, proxy_port=40123)
    assert fence.linked_git == tuple(git) and not any(str(main) in w for w in fence.write_roots)
    forged = FolderGrant(path=str(wt), mode="rw", git=[str(tmp_path)])
    with pytest.raises(GrantRefused):
        registry.create(FrameSpecIn(task_id="t2", kind=FrameKind.MACHINE, grants=[forged]))


async def test_grants_for_names_a_worktrees_git_folders(silent_dispatcher: None, folder: Path) -> None:
    _main, wt = worktree(folder)
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="fix it", folder=str(wt))
    [granted], _ = await grants_grants(workspace.id, task.id)
    assert granted.path == str(wt.resolve()) and granted.git == worktree_git(str(wt))


def test_regrant_changes_the_folders_and_keeps_the_frame(tmp_path: Path) -> None:
    work, extra = tmp_path / "work", tmp_path / "specs"
    work.mkdir()
    extra.mkdir()
    registry = FrameRegistry(tmp_path / "home")
    frame = registry.create(
        FrameSpecIn(task_id="t1", kind=FrameKind.MACHINE, grants=[FolderGrant(path=str(work), mode="rw")])
    )
    token, journal = frame.token, frame.journal
    assert frame.paths.grants == {} and not frame.spec.docker
    told = GrantsIn(
        grants=[FolderGrant(path=str(work), mode="rw"), FolderGrant(path=str(extra), mode="ro")], docker=True
    )
    registry.regrant(frame, told)
    assert frame.paths.grants == {"/grants/specs": str(extra.resolve())} and frame.granted("ro") == [
        str(extra.resolve())
    ]
    assert frame.token == token and frame.journal is journal, "the same frame: its egress and journal stay"
    assert frame.docker_config.is_dir()
    env = device_exec.environment(frame, "http://proxy")
    assert env["DOCKER_CONFIG"] == str(frame.docker_config)
    assert (
        str(frame.docker_config) in device_exec.fence_for(frame, system=False, proxy_port=40123).write_roots
    )
    out = registry.out(frame, "Mac")
    assert out.grants == told.grants and out.docker is True
    assert FrameRegistry(tmp_path / "home").frames[frame.ref].spec.grants == told.grants, "kept in frame.json"

    registry.regrant(frame, GrantsIn())  # everything revoked: the frame works in its own fresh folder
    assert frame.paths.work == str(frame.base / "work") and Path(frame.paths.work).is_dir()
    assert frame.paths.grants == {} and "DOCKER_CONFIG" not in device_exec.environment(frame, "http://proxy")
