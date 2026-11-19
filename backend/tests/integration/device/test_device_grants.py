# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Grants and the task rules of the device: a task on the device
works in the folder a person grants it (resolved, checked), one live task per folder, never in a
Project with members, siblings one by one, a fork copying no folder - and every grant reaches the
frame at its next bind, a revoked one gone.

A device task here is one whose driver resolves to `device` (`machines.driver_of`): the worker's
`frame_driver` is set to it, outside local mode, so a Project may still have members. This file is the
folder: what is granted, what is refused, and when it is busy. The rig is `tests/support/rigs/device_grants_rig.py`."""

from __future__ import annotations

import asyncio
import tempfile
import unicodedata
import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from typer.testing import CliRunner

from prismoid.api.app import create_app
from prismoid.conf import settings
from prismoid.db.models.device_grant import DeviceGrant
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.device import exec as device_exec
from prismoid.device import seatbelt
from prismoid.device.frames import FrameRegistry, GrantRefused, worktree_git
from prismoid.errors import FolderBusy, FolderRefused, ValidationFailed
from prismoid.operations import device_grants
from prismoid.operations import tasks as task_ops
from prismoid.runner import client
from prismoid.runner.main import app as cli
from prismoid.schemas.device import FolderGrant, FrameSpecIn
from prismoid.schemas.device_grants import GrantKind, GrantMode, folder_key
from prismoid.schemas.machines import FrameKind
from tests.support.cast import bound_scope, member
from tests.support.rigs.device_grants_rig import USER_HOME, grants_grants, home_folder, person, worktree_at
from tests.support.rigs.device_grants_rig import device_worker as device_worker
from tests.support.rigs.device_grants_rig import folder as folder
from tests.support.rigs.device_grants_rig import other_folder as other_folder

pytestmark = pytest.mark.integration


async def queued_for_a_lock() -> None:
    """Until a backend of this database waits on an advisory lock: the race is on."""
    waiting = text(
        "select count(*) from pg_stat_activity where datname = current_database() "
        "and wait_event_type = 'Lock' and wait_event = 'advisory'"
    )
    async with asyncio.timeout(10):
        while True:
            async with session_scope() as s:
                if await s.scalar(waiting):
                    return
            await asyncio.sleep(0.01)


def test_folder_defaults_to_cwd_in_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`prismoid do` sends the folder it runs in - only to a deployment whose tasks run on this
    computer; a cloud Prismoid is never told a folder of the person's. `--folder` is always sent."""
    sent: list[dict[str, Any]] = []
    device = {"on": True}

    def fake_request(method: str, path: str, **kw: Any) -> Any:
        if path == "/healthz":
            return {"status": "ok", "environment": "local", "device": device["on"]}
        sent.append(kw["body"])
        return {"task_id": "t"}

    monkeypatch.setattr(client, "request", fake_request)
    monkeypatch.chdir(tmp_path)
    for command in (["do"], ["tasks", "create"]):
        result = CliRunner().invoke(cli, [*command, "fix the build", "--no-wait", "--json"])
        assert result.exit_code == 0, result.output
        assert sent.pop()["folder"] == str(Path.cwd())

    device["on"] = False
    result = CliRunner().invoke(cli, ["do", "fix the build", "--no-wait"])
    assert result.exit_code == 0, result.output
    assert "folder" not in sent.pop(), "never the cwd to a cloud deployment"
    result = CliRunner().invoke(cli, ["do", "x", "--no-wait", "--folder", str(tmp_path), "--docker"])
    assert result.exit_code == 0, result.output
    body = sent.pop()
    assert body["folder"] == str(tmp_path) and body["docker"] is True


async def test_folder_grant_resolves_realpath(silent_dispatcher: None, folder: Path) -> None:
    """A folder reached through a link is granted as the folder it points at, and a
    link into a secret is refused as the secret it is."""
    real = folder / "demo"
    real.mkdir()
    link = folder / "link"
    link.symlink_to(real)
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="fix it", folder=str(link))
        rows = list(await s.scalars(select(DeviceGrant).where(DeviceGrant.task_id == task.id)))
        assert [(r.kind, r.path, r.mode) for r in rows] == [("folder", str(real), "rw")]
        workspace_id, task_id = workspace.id, task.id
    folders, docker = await grants_grants(workspace_id, task_id)
    assert folders == [FolderGrant(path=str(real), mode="rw", name="/work")] and docker is False

    secret = folder / "keys"
    secret.symlink_to(USER_HOME / ".ssh")
    with pytest.raises(FolderRefused):
        await device_grants.validate_folder(str(secret))


@pytest.mark.parametrize(
    "path",
    ["/", "~", "/System/Library", "/Library", "/usr/bin", "/bin", "/sbin", "/private/tmp", "/Applications"],
)
async def test_folder_refused_for_root_home_system_and_deny_list(path: str) -> None:
    with pytest.raises(FolderRefused) as refused:
        await device_grants.validate_folder(path)
    assert refused.value.code == "folder_refused" and refused.value.fields["folder"]


async def test_folder_refused_for_secrets_and_prismoid_home(
    monkeypatch: pytest.MonkeyPatch, folder: Path, silent_dispatcher: None
) -> None:
    """Equal to, inside or holding a `READ_DENY` folder, or Prismoid's own data, and through the API a
    422 `folder_refused` - never a task."""
    for secret in (
        USER_HOME / ".ssh",
        USER_HOME / ".config" / "gh" / "x",
        USER_HOME / "Library" / "Keychains",
    ):
        with pytest.raises(FolderRefused):
            await device_grants.validate_folder(str(secret))
    assert device_grants.folder_refusal(str(USER_HOME / ".aws"), user_home=str(USER_HOME))
    # Every shipped `READ_DENY` folder is hidden or in ~/Library, refused before the list is read: a
    # secret folder anywhere else is refused by the list itself, holding it or inside it.
    fake_home = str(folder)
    (folder / "code" / "vault").mkdir(parents=True)
    assert device_grants.folder_refusal(str(folder / "code"), user_home=fake_home) is None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(seatbelt, "READ_DENY", (*seatbelt.READ_DENY, "~/code/vault"))
        for near in (folder / "code", folder / "code" / "vault", folder / "code" / "vault" / "x"):
            assert "secrets" in (device_grants.folder_refusal(str(near), user_home=fake_home) or ""), near
    prismoid_home = folder / "code" / "prismoid-data"
    prismoid_home.mkdir()
    monkeypatch.setattr(settings.local, "home", prismoid_home)
    for refused in (prismoid_home, folder / "code"):  # Prismoid's own data, and a folder holding it
        with pytest.raises(FolderRefused, match="Prismoid's own data"):
            await device_grants.validate_folder(str(refused))
    with pytest.raises(FolderRefused, match="absolute"):
        await device_grants.validate_folder("code")
    with pytest.raises(FolderRefused, match="not a folder"):
        await device_grants.validate_folder(str(folder / "missing"))

    async with session_scope() as s:
        from prismoid.operations import auth as auth_ops

        made = await auth_ops.signup(
            s, email=f"e1{uuid.uuid4().hex[:8]}@example.test", name="E", password="a-long-enough-password"
        )
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as http:
        h = {"Authorization": f"Bearer {made.token}"}
        bad = await http.post("/v1/tasks", json={"goal": "g", "folder": str(USER_HOME)}, headers=h)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "folder_refused"


async def test_task_without_folder_gets_fresh_work(silent_dispatcher: None, tmp_path: Path) -> None:
    """No folder (the web, a routine, a bot, a fork): no grant, and the helper makes the task a fresh
    `work/` in its own folder - a throwaway frame is never granted one at all."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="research")
    assert await grants_grants(workspace.id, task.id) == ([], False)
    registry = FrameRegistry(tmp_path / "home")
    frame = registry.create(FrameSpecIn(task_id=str(task.id), kind=FrameKind.MACHINE))
    assert frame.paths.work == str(frame.base / "work") and Path(frame.paths.work).is_dir()  # noqa: ASYNC240 - test check
    assert frame.paths.grants == {} and frame.granted("rw") == []
    (tmp_path / "proj").mkdir()
    grant = FolderGrant(path=str(tmp_path / "proj"), mode="rw")
    seat = registry.create(FrameSpecIn(task_id=str(task.id), kind=FrameKind.SEAT, grants=[grant]))
    assert seat.granted("rw") == [] and seat.paths.work == str(seat.base / "work")


async def test_a_folder_or_docker_is_refused_for_a_cloud_task(
    monkeypatch: pytest.MonkeyPatch, silent_dispatcher: None, folder: Path
) -> None:
    monkeypatch.setattr(settings.worker, "frame_driver", "docker")
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        with pytest.raises(ValidationFailed, match="on your computer"):
            await task_ops.create_task(s, ada, goal="g", folder=str(folder))
        with pytest.raises(ValidationFailed, match="on your computer"):
            await task_ops.create_task(s, ada, goal="g", docker=True)
        task = await task_ops.create_task(s, ada, goal="g")
        assert (await s.scalars(select(DeviceGrant).where(DeviceGrant.task_id == task.id))).all() == []


async def test_second_live_task_on_same_folder_is_folder_busy(silent_dispatcher: None, folder: Path) -> None:
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        first = await task_ops.create_task(
            s, ada, goal="fix the login page", folder=str(folder), title="Login fix"
        )
        with pytest.raises(FolderBusy) as busy:
            await task_ops.create_task(s, ada, goal="another", folder=str(folder))
        assert busy.value.code == "folder_busy"
        assert str(first.id) in busy.value.message and "Login fix" in busy.value.message
        bo = await person(s, "Bo")
        with pytest.raises(FolderBusy) as theirs:
            await task_ops.create_task(s, bo, goal="mine", folder=str(folder))
        assert str(first.id) not in theirs.value.message, "another person's task is never named"
        assert "Login fix" not in theirs.value.message

        first.status = "cancelled"  # settled: the folder is free again
        await s.commit()
        again = await task_ops.create_task(s, ada, goal="try again", folder=str(folder))
        assert again.id != first.id


async def test_a_follow_up_on_a_settled_task_whose_folder_was_taken_is_folder_busy(
    silent_dispatcher: None, folder: Path
) -> None:
    """A delivered task's folder is free; when a newer task takes it,
    a follow-up turn on the old one is `folder_busy` naming the newer - and goes once it settles."""
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        a = await task_ops.create_task(s, ada, goal="first pass", folder=str(folder), title="First pass")
        a.status = "delivered"
        await s.commit()
        b = await task_ops.create_task(s, ada, goal="second pass", folder=str(folder), title="Second pass")
        with pytest.raises(FolderBusy) as busy:
            await task_ops.continue_task(s, ada, a.id, goal="one more change")
        assert str(b.id) in busy.value.message and "Second pass" in busy.value.message
        b.status = "cancelled"
        await s.commit()
        again = await task_ops.continue_task(s, ada, a.id, goal="one more change")
        assert again.id == a.id and again.turn == 2


async def test_overlapping_folders_are_busy_on_a_path_boundary(silent_dispatcher: None, folder: Path) -> None:
    """A folder inside, or around, one a live task holds is busy either
    way round; a sibling whose name only starts the same (`/a/bc` beside `/a/b`) is not."""
    for sub in ("b", "b/inner", "bc", "c", "c/inner"):
        (folder / sub).mkdir(parents=True, exist_ok=True)
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        parent = await task_ops.create_task(s, ada, goal="parent", folder=str(folder / "b"), title="Parent")
        with pytest.raises(FolderBusy) as inside:
            await task_ops.create_task(s, ada, goal="child", folder=str(folder / "b" / "inner"))
        assert str(parent.id) in inside.value.message
        sibling = await task_ops.create_task(s, ada, goal="sibling", folder=str(folder / "bc"))
        assert sibling.id != parent.id, "/a/bc is not inside /a/b"

        child = await task_ops.create_task(
            s, ada, goal="child", folder=str(folder / "c" / "inner"), title="Child"
        )
        with pytest.raises(FolderBusy) as around:
            await task_ops.create_task(s, ada, goal="parent", folder=str(folder / "c"))
        assert str(child.id) in around.value.message
        with pytest.raises(FolderBusy):  # a grant added later is held to the same rule
            await device_grants.add_grant(
                s, ada, sibling.id, kind=GrantKind.FOLDER, path=str(folder / "b" / "inner")
            )
        ro = await device_grants.add_grant(
            s, ada, sibling.id, kind=GrantKind.FOLDER, path=str(folder / "b"), mode=GrantMode.RO
        )
        assert ro.mode == "ro", "reading a folder another task works in is not a claim"


def _spelled(path: Path) -> Path:
    """`path` as another spelling names it - on macOS the same folder (APFS ignores case and Unicode
    form), elsewhere made so: either way the second spelling is a folder."""
    path.mkdir(parents=True, exist_ok=True)
    return path


async def test_one_folder_is_busy_whatever_its_case_or_unicode_form(
    silent_dispatcher: None, folder: Path
) -> None:
    """The identity is a key computed in Python (NFC, case-folded) and
    stored; SQL compares keys only - local mode's Postgres is C-collated, and its lower() leaves
    `Проекты` as it is."""
    assert folder_key("/Users/x/Проекты/École") == folder_key("/users/x/проекты/école")
    nfd = unicodedata.normalize("NFD", "/Users/x/École")
    assert nfd != "/Users/x/École" and folder_key(nfd) == folder_key("/Users/x/École")
    assert folder_key("/a/b") != folder_key("/a/bc")
    capital = _spelled(folder / "Проекты")
    composed = _spelled(folder / unicodedata.normalize("NFC", "École"))
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        first = await task_ops.create_task(s, ada, goal="cyrillic", folder=str(capital))
        accented = await task_ops.create_task(s, ada, goal="accented", folder=str(composed))
        lower = _spelled(folder / "проекты")
        with pytest.raises(FolderBusy) as by_case:
            await task_ops.create_task(s, ada, goal="again", folder=str(lower))
        assert str(first.id) in by_case.value.message
        decomposed = _spelled(folder / unicodedata.normalize("NFD", "École"))
        with pytest.raises(FolderBusy) as by_form:
            await task_ops.create_task(s, ada, goal="again", folder=str(_spelled(decomposed / "src")))
        assert str(accented.id) in by_form.value.message
        rows = (await s.scalars(select(DeviceGrant).where(DeviceGrant.task_id == first.id))).all()
        assert [r.path_key for r in rows] == [folder_key(r.path or "") for r in rows]


async def test_a_grants_name_is_minted_once_and_never_reused(
    silent_dispatcher: None, folder: Path, tmp_path: Path
) -> None:
    """A, B and C granted, B revoked, D granted: A and C keep their
    names, D gets a fresh one, and B's stays retired - even for a folder of the same name. A revoked
    `/work` is never handed to another folder: the frame works in a fresh one of its own."""
    for sub in ("work", "a", "b", "c", "d/b", "e"):
        (folder / sub).mkdir(parents=True)
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="names", folder=str(folder / "work"))
        a, b, c = [
            await device_grants.add_grant(
                s, ada, task.id, kind=GrantKind.FOLDER, path=str(folder / n), mode=GrantMode.RO
            )
            for n in ("a", "b", "c")
        ]
        assert (a.name, b.name, c.name) == ("/grants/a", "/grants/b", "/grants/c")
        await device_grants.revoke_grant(s, ada, b.id)
        d = await device_grants.add_grant(
            s, ada, task.id, kind=GrantKind.FOLDER, path=str(folder / "d" / "b"), mode=GrantMode.RO
        )
        assert d.name == "/grants/b_2", "b's name stays retired"
        work = next(g for g in await device_grants.list_grants(s, ada, task.id) if g.name == "/work")
        await device_grants.revoke_grant(s, ada, work.id)
        e = await device_grants.add_grant(s, ada, task.id, kind=GrantKind.FOLDER, path=str(folder / "e"))
        assert e.name == "/grants/e", "a later read-write folder never becomes /work"
        ids = workspace.id, task.id
    folders, _ = await grants_grants(*ids)
    assert [(g.name, Path(g.path).name) for g in folders] == [
        ("/grants/a", "a"),
        ("/grants/c", "c"),
        ("/grants/b_2", "b"),
        ("/grants/e", "e"),
    ]
    frame = FrameRegistry(tmp_path / "home").create(
        FrameSpecIn(task_id=str(ids[1]), kind=FrameKind.MACHINE, grants=folders)
    )
    assert frame.paths.work == str(frame.base / "work"), "no /work grant: a fresh folder, never e"
    assert frame.paths.grants["/grants/b_2"] == str((folder / "d" / "b").resolve())
    assert (
        str(frame.base / "work") in device_exec.fence_for(frame, system=False, proxy_port=40123).write_roots
    )


async def test_a_revoked_folder_is_held_not_worked_in_until_its_task_settles(
    silent_dispatcher: None, folder: Path
) -> None:
    """A task keeps a revoked folder until its next bind, so the folder stays busy; the message says
    what is true, to its creator and to anyone else."""
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        first = await task_ops.create_task(s, ada, goal="fix the login page", folder=str(folder), title="Fix")
        row = await s.scalar(select(DeviceGrant).where(DeviceGrant.task_id == first.id))
        assert row is not None
        await device_grants.revoke_grant(s, ada, row.id)
        with pytest.raises(FolderBusy) as busy:
            await task_ops.create_task(s, ada, goal="another", folder=str(folder))
        assert "still holds" in busy.value.message and "until it settles" in busy.value.message
        assert "is working in" not in busy.value.message and str(first.id) in busy.value.message
        bo = await person(s, "Bo")
        with pytest.raises(FolderBusy) as theirs:
            await task_ops.create_task(s, bo, goal="mine", folder=str(folder))
        assert "still holds" in theirs.value.message and "is working in" not in theirs.value.message
        assert str(first.id) not in theirs.value.message


async def test_a_revoked_folder_stays_busy_while_its_task_is_live(
    silent_dispatcher: None, folder: Path
) -> None:
    """Its frame keeps the folder until its next bind: another task may not take it meanwhile."""
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        first = await task_ops.create_task(s, ada, goal="first", folder=str(folder))
        [grant] = await device_grants.list_grants(s, ada, first.id)
        await device_grants.revoke_grant(s, ada, grant.id)
        with pytest.raises(FolderBusy):
            await task_ops.create_task(s, ada, goal="second", folder=str(folder))
        first.status = "delivered"
        await s.commit()
        await task_ops.create_task(s, ada, goal="third", folder=str(folder))


async def test_a_worktree_of_a_repository_prismoid_cannot_write_is_refused_by_its_name(
    silent_dispatcher: None, folder: Path
) -> None:
    """A worktree whose repository lives in a hidden home folder - one
    the fence never lets git write - is refused when it is granted, `folder_refused` naming only
    the grant's virtual root; the helper refuses it the same way, naming no path."""
    hidden = home_folder(".prismoid-e1-repos-")
    _main, wt = worktree_at(hidden / "main", folder / "wt")
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        with pytest.raises(FolderRefused) as at_create:
            await task_ops.create_task(s, ada, goal="in the worktree", folder=str(wt))
        task = await task_ops.create_task(s, ada, goal="elsewhere")
        with pytest.raises(FolderRefused) as at_grant:
            await device_grants.add_grant(s, ada, task.id, kind=GrantKind.FOLDER, path=str(wt))
        await device_grants.add_grant(
            s, ada, task.id, kind=GrantKind.FOLDER, path=str(wt), mode=GrantMode.RO
        )  # reads only
    for refused, name in ((at_create, "/work"), (at_grant, "/grants/wt")):
        message = refused.value.message
        assert message.startswith(f"{name} is a git worktree") and str(hidden) not in message
        assert str(USER_HOME) not in message and "/Users" not in message, message
    registry = FrameRegistry(Path(tempfile.mkdtemp(dir=folder)) / "home")
    spec = FrameSpecIn(
        task_id="t1",
        kind=FrameKind.MACHINE,
        grants=[FolderGrant(path=str(wt), mode="rw", name="/work", git=worktree_git(str(wt)))],
    )
    with pytest.raises(GrantRefused) as helper:
        registry.create(spec)
    assert str(helper.value).startswith("/work: its repository's git folder") and str(hidden) not in str(
        helper.value
    )


async def test_a_follow_up_racing_a_grant_add_both_land(
    silent_dispatcher: None, folder: Path, other_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real concurrency: a grant add holds the folder lock while it checks the folder, and its
    insert then needs the task's row. A follow-up that took the row first and queued for the lock
    would wait for it while it waits for the row, and Postgres would abort one of them."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="first pass", folder=str(folder))
        task.status = "delivered"
        await s.commit()
        task_id = task.id
    checking, checked = asyncio.Event(), asyncio.Event()
    validate = device_grants.validate_folder

    async def held_check(path: str) -> str:
        checking.set()
        await checked.wait()
        return await validate(path)

    monkeypatch.setattr(device_grants, "validate_folder", held_check)

    async def add() -> DeviceGrant:
        async with bound_scope(workspace.id) as s:
            grant = await device_grants.add_grant(
                s, ada, task_id, kind=GrantKind.FOLDER, path=str(other_folder)
            )
            await s.commit()
            return grant

    async def follow_up() -> Task:
        async with bound_scope(workspace.id) as s:
            return await task_ops.continue_task(s, ada, task_id, goal="one more change")

    adding = asyncio.create_task(add())
    await asyncio.wait_for(checking.wait(), 10)
    following = asyncio.create_task(follow_up())
    await queued_for_a_lock()
    checked.set()
    grant, continued = await asyncio.wait_for(asyncio.gather(adding, following), 30)
    assert grant.path == str(other_folder) and continued.turn == 2


async def test_concurrent_creates_on_one_folder_one_wins(
    silent_dispatcher: None, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real concurrency: five creates on one folder, each on its own connection, each holding its
    transaction open after the check - exactly one takes the folder."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        await s.commit()
    record = device_grants.record

    async def slow_record(*args: Any, **kw: Any) -> None:
        await record(*args, **kw)
        await asyncio.sleep(0.3)  # the check is done and the grant written, the commit not yet

    monkeypatch.setattr(device_grants, "record", slow_record)

    async def create(n: int) -> Task:
        async with bound_scope(workspace.id) as s:
            return await task_ops.create_task(s, ada, goal=f"race {n}", folder=str(folder))

    results = await asyncio.gather(*(create(n) for n in range(5)), return_exceptions=True)
    won = [r for r in results if isinstance(r, Task)]
    lost = [r for r in results if isinstance(r, FolderBusy)]
    assert len(won) == 1 and len(lost) == 4, results
    async with bound_scope(workspace.id) as s:
        rows = (await s.scalars(select(DeviceGrant).where(DeviceGrant.path == str(folder)))).all()
        assert [r.task_id for r in rows] == [won[0].id]
