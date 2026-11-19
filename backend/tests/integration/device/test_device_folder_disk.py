# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""One folder, one disk (ADR-078): a deployment's device is one computer, so
"is this folder busy" and its lock hold across workspaces - while everything else a claim touches stays
in its own workspace, and a task of another one never enters the claimer's session."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from prismoid.db.models.actor import Actor
from prismoid.db.models.device_grant import DeviceGrant
from prismoid.db.models.task import Task
from prismoid.db.models.workspace import Workspace
from prismoid.db.session import session_scope
from prismoid.errors import FolderBusy, TenantUnbound
from prismoid.operations import device_grants
from prismoid.operations import tasks as task_ops
from prismoid.schemas.device_grants import folder_key
from tests.support.cast import a_workspace, bound_scope, member
from tests.support.rigs.device_grants_rig import device_worker as device_worker
from tests.support.rigs.device_grants_rig import folder as folder
from tests.support.rigs.device_grants_rig import person

pytestmark = pytest.mark.integration


async def _two_workspaces() -> tuple[Actor, Workspace, Actor, Workspace]:
    async with session_scope() as s:
        ada, home = await member(s, "Ada")
        bo, away = await a_workspace(s, "Bo")
        await s.commit()
        return ada, home, bo, away


async def test_a_folder_held_in_one_workspace_is_busy_in_another(
    silent_dispatcher: None, folder: Path
) -> None:
    ada, home, bo, away = await _two_workspaces()
    async with bound_scope(home.id) as s:
        first = await task_ops.create_task(s, ada, goal="fix", folder=str(folder), title="Login fix")
    async with bound_scope(away.id) as s:
        with pytest.raises(FolderBusy) as theirs:
            await task_ops.create_task(s, bo, goal="mine", folder=str(folder))
    assert str(first.id) not in theirs.value.message, "another person's task is never named"
    assert "Login fix" not in theirs.value.message


async def test_a_folder_around_or_inside_is_busy_across_workspaces(
    silent_dispatcher: None, folder: Path
) -> None:
    (folder / "inner").mkdir()
    ada, home, bo, away = await _two_workspaces()
    async with bound_scope(home.id) as s:
        await task_ops.create_task(s, ada, goal="fix", folder=str(folder / "inner"))
    async with bound_scope(away.id) as s:
        with pytest.raises(FolderBusy):
            await task_ops.create_task(s, bo, goal="around it", folder=str(folder))


async def test_the_same_person_is_told_their_task_is_in_another_workspace(
    silent_dispatcher: None, folder: Path
) -> None:
    ada, home, _, away = await _two_workspaces()
    async with bound_scope(home.id) as s:
        first = await task_ops.create_task(s, ada, goal="fix", folder=str(folder), title="Login fix")
    async with bound_scope(away.id) as s:
        with pytest.raises(FolderBusy) as busy:
            await device_grants.claim_folder(s, ada, str(folder))
    assert str(first.id) in busy.value.message and "in another workspace" in busy.value.message


async def test_a_settled_task_frees_the_folder_for_every_workspace(
    silent_dispatcher: None, folder: Path
) -> None:
    ada, home, bo, away = await _two_workspaces()
    async with bound_scope(home.id) as s:
        first = await task_ops.create_task(s, ada, goal="fix", folder=str(folder))
        first.status = "cancelled"
        await s.commit()
    async with bound_scope(away.id) as s:
        second = await task_ops.create_task(s, bo, goal="mine", folder=str(folder))
    assert second.id != first.id


async def test_the_read_leaves_the_other_workspaces_row_out_of_the_session(
    silent_dispatcher: None, folder: Path
) -> None:
    ada, home, _, away = await _two_workspaces()
    async with bound_scope(home.id) as s:
        first = await task_ops.create_task(s, ada, goal="fix", folder=str(folder))
    async with bound_scope(away.id) as s:
        found = await DeviceGrant.folder_holder(s, folder_key(str(folder)))
        assert found is not None and found.task_id == first.id and found.workspace_id == home.id
        assert not [row for row in s.identity_map.values() if isinstance(row, Task)]


async def test_the_read_still_needs_a_door_to_have_bound_a_workspace(folder: Path) -> None:
    async with session_scope() as s:
        with pytest.raises(TenantUnbound):
            await DeviceGrant.folder_holder(s, folder_key(str(folder)))


async def test_two_workspaces_claims_queue_on_one_lock(
    silent_dispatcher: None, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workspace A's grant is written and not yet committed. B's claim on the same folder must wait
    for it and then lose: a lock per workspace lets B past a grant it cannot see yet."""
    ada, home, bo, away = await _two_workspaces()
    written = asyncio.Event()
    commit = asyncio.Event()
    record = device_grants.record

    async def record_and_hold(*args: Any, **kw: Any) -> None:
        await record(*args, **kw)
        written.set()
        await commit.wait()

    monkeypatch.setattr(device_grants, "record", record_and_hold)

    async def first() -> Task:
        async with bound_scope(home.id) as s:
            return await task_ops.create_task(s, ada, goal="fix", folder=str(folder))

    async def second() -> None:
        async with bound_scope(away.id) as s:
            await device_grants.claim_folder(s, bo, str(folder))

    async def a_claim_waits() -> bool:
        async with session_scope() as s:
            query = text("select count(*) from pg_locks where locktype = 'advisory' and not granted")
            return bool(await s.scalar(query))

    claimer = asyncio.create_task(first())
    await written.wait()
    rival = asyncio.create_task(second())
    for _ in range(500):  # until the rival stands in line, or never needed to
        if rival.done() or await a_claim_waits():
            break
        await asyncio.sleep(0.01)
    commit.set()
    await claimer
    with pytest.raises(FolderBusy):
        await rival


async def test_a_task_in_the_same_workspace_is_still_busy_for_a_teammate(
    silent_dispatcher: None, folder: Path
) -> None:
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        await task_ops.create_task(s, ada, goal="fix", folder=str(folder))
        bo = await person(s, "Bo")
        with pytest.raises(FolderBusy):
            await task_ops.create_task(s, bo, goal="mine", folder=str(folder))


async def test_task_by_ids_keeps_to_the_bound_workspace(silent_dispatcher: None) -> None:
    ada, home, bo, away = await _two_workspaces()
    async with bound_scope(home.id) as s:
        mine = await task_ops.create_task(s, ada, goal="mine")
    async with bound_scope(away.id) as s:
        theirs = await task_ops.create_task(s, bo, goal="theirs")
        assert [t.id for t in await Task.by_ids(s, {mine.id, theirs.id})] == [theirs.id]
    async with session_scope() as s:
        with pytest.raises(TenantUnbound):
            await Task.by_ids(s, {mine.id})
