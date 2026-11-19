# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Grants and the task rules of the device: what a person lets a task
on their own computer reach, and the two rules a device task keeps.

- **Grants** are rows (`device_grants`), Postgres the authority: a folder (`--folder` at create is
  the task's read-write work folder; more, read-write or read-only, by `prismoid device grants add`) and
  Docker. The worker tells the helper at a frame's create and bind (`grants_for`); only a person
  grants, and a folder is checked first (`validate_folder`): never `/`, the home, a system folder,
  or one holding or inside a secret.
- **One live task per folder**: a read-write folder another live task holds - or one inside
  or around it - is `folder_busy`, decided under one advisory lock for every folder claim (a create,
  a grant, a follow-up turn), so two claims can never both take overlapping folders. A deployment's
  device is one computer, so the rule and its lock hold across workspaces.
- **Private-only**: a device task and a Project with members besides its owner never meet.
  `_has_members` is the one predicate ("only the owner drives", O6), so shared tasks can lift it.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pwd
import re
import uuid
from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.device_grant import DeviceGrant
from prismoid.db.models.project import Project
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.task import Task
from prismoid.device.fence_data import READ_DENY_PATTERNS
from prismoid.device.frames import worktree_git
from prismoid.device.seatbelt import home_patterns, linked_git_refusal, read_denied, under
from prismoid.errors import DeviceTaskPrivate, FolderBusy, FolderRefused, Forbidden, ValidationFailed
from prismoid.schemas.device import DEVICE_DRIVER
from prismoid.schemas.device_grants import (
    WORK_NAME,
    FolderGrant,
    GrantKind,
    GrantMode,
    folder_key,
    mint_grant_name,
    write_root_refusal,
)
from prismoid.schemas.machines import FrameDriverName

# Never a grant, whatever lies under them: the system's own folders (`/private` holds `/tmp`, `/var`
# and `/etc`). Outside the home every path is refused anyway (`write_root_refusal`); these are named
# so the refusal says why.
SYSTEM_FOLDERS: Final = ("/System", "/Library", "/usr", "/bin", "/sbin", "/private", "/Applications")


def new_task_driver() -> FrameDriverName | None:
    """The driver a new task records as its own (`tasks.machine_driver`): `device` in local mode,
    where this computer is the only Machine; None - the worker's - everywhere else."""
    return DEVICE_DRIVER if str(settings.environment) == "local" else None


def on_device(machine_driver: str | None) -> bool:
    """A task recording `machine_driver` runs on the device, by `machines.driver_of`'s own rule."""
    from prismoid.operations.machines import driver_named  # cycle: the machines ask this module for grants

    return driver_named(machine_driver) == DEVICE_DRIVER


def new_tasks_on_device() -> bool:
    """A task created now runs on the device: the CLI's `--folder` defaults to its cwd only then."""
    return on_device(new_task_driver())


# ── the folder ────────────────────────────────────────────────────────────────────────────────


def _user_home() -> str:
    return os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)


def folder_refusal(real: str, *, user_home: str | None = None) -> str | None:
    """Why the real folder `real` may not be granted, or None. Compared ignoring case, as macOS
    volumes and the fence do. The home and write-root rule is the fence's own (`write_root_refusal`)."""
    home = user_home or _user_home()
    if real == "/":
        return "/ is the whole disk"
    if system := next((s for s in SYSTEM_FOLDERS if under(real, s)), None):
        return f"{real} is in {system}, a system folder"
    if refusal := write_root_refusal(real, home):
        return refusal
    for secret in read_denied(user_home=home):
        if under(real, secret) or under(secret, real):
            return f"{real} holds or is inside a folder of secrets ({secret})"
    if any(re.search(p, real, re.IGNORECASE) for p in home_patterns(READ_DENY_PATTERNS, user_home=home)):
        return f"{real} is inside a folder of secrets"
    prismoid_home = os.path.realpath(settings.local.home.expanduser())
    if under(real, prismoid_home) or under(prismoid_home, real):
        return f"{real} holds or is inside Prismoid's own data ({prismoid_home})"
    return None


def _on_disk(real: str) -> str:
    """`real` as the disk spells it. On macOS `realpath` keeps a typed case (`~/projects` for
    `~/Projects`); the kernel's own name for the open folder (`F_GETPATH`) does not. As it is
    elsewhere."""
    get = getattr(fcntl, "F_GETPATH", None)
    if get is None:
        return real
    try:
        fd = os.open(real, os.O_RDONLY | os.O_DIRECTORY)
        try:
            raw = fcntl.fcntl(fd, get, bytes(1024))
        finally:
            os.close(fd)
    except OSError:
        return real
    return os.fsdecode(raw.split(b"\0", 1)[0]) or real


def _validated(path: str) -> str:
    if not path.startswith(("/", "~")):
        raise FolderRefused(f"{path}: not an absolute path", fields={"folder": "must be absolute"})
    real = os.path.realpath(os.path.expanduser(path))
    if refusal := folder_refusal(real):
        raise FolderRefused(refusal, fields={"folder": refusal})
    if not os.path.isdir(real):
        raise FolderRefused(f"{real} is not a folder", fields={"folder": "not a folder"})
    real = _on_disk(real)
    if refusal := folder_refusal(real):
        raise FolderRefused(refusal, fields={"folder": refusal})
    return real


async def validate_folder(path: str) -> str:
    """The folder a person grants, as its real path (links resolved, so a grant reached through a
    link is the folder it points at; spelled as the disk spells it) - or `FolderRefused` for `/`,
    the home, a system folder, a folder outside the home and external disks, and any folder equal
    to, inside or holding a secret (`READ_DENY`) or Prismoid's own data."""
    return await asyncio.to_thread(_validated, path)


def _worktree_refusal(real: str, name: str) -> str | None:
    prismoid_home = settings.local.home.expanduser()
    for git in worktree_git(real):
        if why := linked_git_refusal(git, prismoid_home):
            return (
                f"{name} is a git worktree whose repository's git folder {why}: git could not work "
                "there - grant the repository itself, or a worktree of one Prismoid may write"
            )
    return None


async def validate_worktree(real: str, name: str) -> None:
    """A read-write folder that is a git worktree needs its repository's git folders writable for
    git's state files: when the fence would refuse one, the grant is refused
    now - `FolderRefused` naming the grant by its virtual root, never the repository's real path -
    rather than every command of the task failing later. The fence's own rule (`linked_git_refusal`)."""
    if why := await asyncio.to_thread(_worktree_refusal, real, name):
        raise FolderRefused(why, fields={"folder": why})


_LET_IT_END: Final = "let it finish, or cancel it"


async def claim_folder(
    session: AsyncSession, actor: Actor, real: str, *, task_id: uuid.UUID | None = None, lock: bool = True
) -> None:
    """One live task per folder: `FolderBusy` when another live task holds `real`, a
    folder inside it or one around it, read-write, in any workspace - compared by `folder_key`. Under
    the folder-claim lock, held to the caller's commit: two claims queue here, and the second sees the
    first's grant. `lock=False` is the early read a caller makes before its slow side effects; it
    decides nothing, and the locked claim follows. The message names the other task to its creator
    alone - a device task is private."""
    if lock:
        await DeviceGrant.lock_folders(session)
    key = folder_key(real)
    found = await DeviceGrant.folder_holder(session, key, other_than=task_id)
    if found is None:
        return
    where = real if folder_key(found.path) == key else f"{found.path}, which overlaps {real}"
    if found.creator_actor_id == actor.id:
        title = next(iter(found.label.strip().splitlines()), "")[:60]
        named = f'task {found.task_id} ("{title}")'
        if found.workspace_id != tenant.workspace_of(session):
            named += " in another workspace"
        if found.revoked:
            raise FolderBusy(f"{named} still holds {where}, revoked, until it settles: {_LET_IT_END}")
        raise FolderBusy(f"{named} is working in {where}: {_LET_IT_END}")
    if found.revoked:
        raise FolderBusy(f"another task still holds {real} or a folder around or inside it until it settles")
    raise FolderBusy(f"another task is working in {real} or a folder around or inside it")


async def reclaim_folders(session: AsyncSession, actor: Actor, task: Task, *, lock: bool = True) -> None:
    """A settled device task going live again (a follow-up turn): its read-write folders must still
    be free - another task may have taken one while it was settled. The caller holds the task's row
    (`lock=False`: the early read before its slow side effects)."""
    if not on_device(task.machine_driver):
        return
    for grant in await DeviceGrant.live_for_task(session, task.id):
        if grant.kind == GrantKind.FOLDER and grant.mode == GrantMode.RW and grant.path is not None:
            await claim_folder(session, actor, grant.path, task_id=task.id, lock=lock)


async def lock_task(session: AsyncSession, task: Task) -> None:
    """The task's row, re-read under `FOR UPDATE` and held to the caller's commit. On the device the
    folder lock comes first and stays held too: `add_grant` and a fork hold it while their inserts
    wait for this row, so a caller that took the row first and then a claim would wait on them while
    they wait on it."""
    if on_device(task.machine_driver):
        await DeviceGrant.lock_folders(session)
    await session.refresh(task, with_for_update=True)


# ── a new task's place ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Placement:
    """Where a new task runs: the driver its row records, and on the device the folder and Docker
    it was given. The three calls below are `create_task`'s whole dealing with the device."""

    machine_driver: FrameDriverName | None
    on_device: bool
    work_folder: str | None
    docker: bool


async def place_task(
    session: AsyncSession, actor: Actor, project: Project | None, *, folder: str | None, docker: bool
) -> Placement:
    """In local mode every task's Machine is this computer. The folder a person names is its work
    folder - checked here, before the caller stores anything - and a folder or Docker means nothing
    to a task on a cloud Machine. The two device rules are read early, so a refusal leaves no
    orphaned attachment; they decide again under their locks in `hold_place`."""
    machine_driver = new_task_driver()
    device = on_device(machine_driver)
    if (folder is not None or docker) and not device:
        raise ValidationFailed(
            "a folder or Docker is granted only to a task on your computer",
            fields={"folder" if folder is not None else "docker": "not a task on your computer"},
        )
    work_folder = await validate_folder(folder) if folder is not None else None
    if work_folder is not None:
        await validate_worktree(work_folder, WORK_NAME)
    if device and project is not None:
        await keep_private(session, project, lock=False)
    if work_folder is not None:
        await claim_folder(session, actor, work_folder, lock=False)
    return Placement(machine_driver, device, work_folder, docker)


async def hold_place(
    session: AsyncSession, actor: Actor, project: Project | None, placement: Placement
) -> None:
    """The two device rules, under their locks, held to the caller's commit: a device task never
    sits in a Project with members, and one live task works in a folder."""
    if placement.on_device and project is not None:
        await keep_private(session, project)
    if placement.work_folder is not None:
        await claim_folder(session, actor, placement.work_folder)


async def record_place(session: AsyncSession, task: Task, actor: Actor, placement: Placement) -> None:
    """The new row's grants. No folder: the helper makes the task a fresh one under its own."""
    if placement.on_device:
        await record(session, task, actor, folder=placement.work_folder, docker=placement.docker)


# ── the task's grants ─────────────────────────────────────────────────────────────────────────


async def record(
    session: AsyncSession, task: Task, actor: Actor, *, folder: str | None, docker: bool
) -> None:
    """A new task's grants, in its creating transaction: its work folder (read-write, `/work`) and
    Docker."""
    if folder is not None:
        session.add(_folder_row(task, actor, folder, GrantMode.RW, WORK_NAME))
    if docker:
        session.add(DeviceGrant(task_id=task.id, kind=GrantKind.DOCKER, created_by_actor_id=actor.id))
    await session.flush()


async def grants_for(session: AsyncSession, task_id: uuid.UUID) -> tuple[list[FolderGrant], bool]:
    """The task's live grants as the helper is told them: its folders, oldest first, each
    under the virtual root it was given when granted (`/work` is the work folder), and whether it
    has Docker. A read-write folder that is a git worktree carries the git folders its pointer names."""
    rows = await DeviceGrant.live_for_task(session, task_id)
    folders = []
    for row in rows:
        if row.kind == GrantKind.FOLDER and row.path is not None and row.mode is not None and row.name:
            git = await asyncio.to_thread(worktree_git, row.path) if row.mode == GrantMode.RW else []
            mode: Literal["ro", "rw"] = "rw" if row.mode == GrantMode.RW else "ro"
            folders.append(FolderGrant(path=row.path, mode=mode, name=row.name, git=git))
    return folders, any(row.kind == GrantKind.DOCKER for row in rows)


def _folder_row(task: Task, actor: Actor, real: str, mode: GrantMode, name: str) -> DeviceGrant:
    return DeviceGrant(
        task_id=task.id,
        kind=GrantKind.FOLDER,
        path=real,
        path_key=folder_key(real),
        mode=mode,
        name=name,
        created_by_actor_id=actor.id,
    )


async def _own_device_task(session: AsyncSession, actor: Actor, task_id: uuid.UUID) -> Task:
    """A task the actor may grant to: theirs, and on the device. A person grants only their own."""
    task = await Task.get_for(session, task_id, actor)
    if task.creator_actor_id != actor.id:
        raise Forbidden("only the task's creator grants it folders")
    if not on_device(task.machine_driver):
        raise ValidationFailed(
            "grants are for a task on your computer", fields={"task_id": "not a task on your computer"}
        )
    return task


async def add_grant(
    session: AsyncSession,
    actor: Actor,
    task_id: uuid.UUID,
    *,
    kind: GrantKind,
    path: str | None = None,
    mode: GrantMode = GrantMode.RW,
) -> DeviceGrant:
    """A person grants a task a folder or Docker (`prismoid device grants add`). The frame is told
    before a running step's next tool, or at its next bind. Idempotent on a live grant of the same folder and mode (or on Docker); a folder
    granted again in the other mode replaces the old grant, under a new name. A new folder grant's
    virtual root is minted here, once, against every name the task ever used: it is never re-derived
    and never reused. A read-write folder must be free, and a worktree's git folders writable."""
    task = await _own_device_task(session, actor, task_id)
    await DeviceGrant.lock_folders(session)  # claims and names, one at a time
    live = await DeviceGrant.live_for_task(session, task.id)
    if kind == GrantKind.DOCKER:
        if existing := next((g for g in live if g.kind == GrantKind.DOCKER), None):
            return existing
        row = DeviceGrant(task_id=task.id, kind=GrantKind.DOCKER, created_by_actor_id=actor.id)
    else:
        if path is None:
            raise ValidationFailed("a folder grant needs its path", fields={"path": "required"})
        real = await validate_folder(path)
        key = folder_key(real)
        same = [g for g in live if g.kind == GrantKind.FOLDER and g.path_key == key]
        if kept := next((g for g in same if g.mode == mode), None):
            return kept
        name = mint_grant_name(real, await DeviceGrant.all_names(session, task.id))
        if mode == GrantMode.RW:
            await validate_worktree(real, name)
            await claim_folder(session, actor, real, task_id=task.id)
        for old in same:
            old.revoked_at = utcnow()
        row = _folder_row(task, actor, real, mode, name)
    session.add(row)
    await session.commit()
    return row


async def list_grants(
    session: AsyncSession, actor: Actor, task_id: uuid.UUID | None = None
) -> list[DeviceGrant]:
    """The live grants of the actor's own tasks (one task's, given one), oldest first."""
    return await DeviceGrant.live_of_creator(session, actor.id, task_id)


async def revoke_grant(session: AsyncSession, actor: Actor, grant_id: uuid.UUID) -> None:
    """Soft: the row stays, revoked. The frame loses the folder before a running step's next tool
    (`machines.grant_fence`), or at its next bind."""
    row = await DeviceGrant.get_for_creator(session, grant_id, actor.id)
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        await session.commit()


# ── private-only ──────────────────────────────────────────────────────────────────────────────


async def _has_members(session: AsyncSession, project: Project) -> bool:
    """The Project has members besides its owner. THE predicate of private-only: "only the owner
    drives" (O6) lives here and nowhere else, so shared tasks can lift it in one place."""
    return await ProjectMember.any_besides(session, project.id, project.owner_actor_id)


async def keep_private(session: AsyncSession, project: Project, *, lock: bool = True) -> None:
    """A device task is about to be filed in `project` (a create or a move): `DeviceTaskPrivate`
    when it has members. The Project row is share-locked to the caller's commit, so a member added
    meanwhile (`refuse_members`, which locks it for update) waits and then sees the task.
    `lock=False` is a caller's early read before its slow side effects; the locked check follows."""
    if lock:
        await session.refresh(project, with_for_update={"read": True})
    if await _has_members(session, project):
        raise DeviceTaskPrivate(
            "a task on your computer stays private: this Project has members besides its owner"
        )


async def keep_move_private(session: AsyncSession, task: Task, destination: Project | None) -> None:
    """`task` is moving into `destination` (None: out of any Project), and the caller holds its row:
    a device task stays private, so it never lands in a shared Project."""
    if destination is not None and on_device(task.machine_driver):
        await keep_private(session, destination)


async def _holds_device_task(session: AsyncSession, project: Project) -> bool:
    return any(on_device(driver) for driver in await Task.drivers_in_project(session, project.id))


async def refuse_members(session: AsyncSession, project: Project) -> None:
    """A member is about to join `project`: `DeviceTaskPrivate` when it holds a task on someone's
    computer (the rule keys on "has members"). The Project row is locked for update to the
    caller's commit, so a device task filed meanwhile (`keep_private`) waits and then sees the member."""
    await session.refresh(project, with_for_update=True)
    if await _holds_device_task(session, project):
        raise DeviceTaskPrivate(
            "this Project holds a task on its owner's computer, which stays private: it takes no members"
        )
