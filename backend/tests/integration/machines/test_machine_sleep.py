# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Sleep, Sleep now, reset, expiry and forget."""

import asyncio
import datetime as dt
import io
import uuid
from typing import Any

import pytest
from sqlalchemy import delete, select

from prismoid import storage
from prismoid.ai import frames
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.machine import Machine
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.task import Task
from prismoid.db.models.usage import UsageEvent
from prismoid.db.models.workspace import Workspace, WorkspaceRole
from prismoid.db.session import session_scope
from prismoid.errors import Conflict, Forbidden
from prismoid.operations import machines
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations.machines import store
from prismoid.schemas.machines import MachineState, SleepReason, frame_name, machine_prefix
from tests.support.cast import member
from tests.support.dispatchers import RecordingDispatcher
from tests.support.fakes.fake_frames import NATIVE, NativeFakeFrame, frame_class

pytestmark = pytest.mark.integration


async def _warm(
    fake_frames: Any,
    *,
    state: MachineState = MachineState.IDLE,
    profile: str = "base",
    files: dict[str, bytes] | None = None,
    held_by: str | None = None,
    since_s: int = 0,
    driver: str = "docker",
) -> tuple[uuid.UUID, uuid.UUID, Actor, Workspace]:
    """A task with a warm Machine at epoch 1 on `driver` whose fake frame holds `files`, in `state` for
    `since_s` seconds - by Postgres's clock, the one the claim measures with: stamped from the
    host's, a Docker VM's clock a moment behind it read 89 s of a 90 s idle."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="sleepy")
        since = await Machine.db_now(s) - dt.timedelta(seconds=since_s)
        row = Machine(
            workspace_id=task.workspace_id,
            task_id=task.id,
            driver=driver,
            profile=profile,
            state=state,
            state_since=since,
            last_active_at=since,
            epoch=1,
            held_by=held_by,
        )
        s.add(row)
        await s.flush()
        row.frame_ref = frame_name(row.id, 1)
        await s.commit()
        frame = frame_class(driver)(str(task.id), profile=profile, name=row.frame_ref)
        await frame.create()
        for path, data in (files or {"/work/notes.md": b"hello"}).items():
            await frame.write_bytes(path, data)
        return row.id, task.id, ada, workspace


async def _row(workspace_id: uuid.UUID, machine_id: uuid.UUID) -> Machine | None:
    async with session_scope(workspace_id=workspace_id) as s:
        row = await s.get(Machine, machine_id)
        if row is not None:
            s.expunge(row)  # the scope's closing rollback would expire it
        return row


async def _set_checkpoint(workspace_id: uuid.UUID, machine_id: uuid.UUID) -> None:
    """An earlier sleep's checkpoint on the row (never read here: no wake follows)."""
    async with session_scope(workspace_id=workspace_id) as s:
        row = await s.get(Machine, machine_id)
        assert row is not None
        row.checkpoint_object, row.checkpoint_sha256 = f"machines/{machine_id}/0.tar.gz", "0" * 64
        row.checkpoint_bytes = 1
        await s.commit()


def _capture(
    dispatcher: RecordingDispatcher, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, str, dict[str, Any]]]:
    """Every enqueue with its kwargs (RecordingDispatcher keeps only the job and the key)."""
    sent: list[tuple[str, str, dict[str, Any]]] = []
    record = dispatcher.enqueue

    async def enqueue(job: str, key: str, **kwargs: Any) -> None:
        sent.append((job, key, kwargs))
        await record(job, key, **kwargs)

    monkeypatch.setattr(dispatcher, "enqueue", enqueue)
    return sent


def _claim_of(kw: dict[str, Any]) -> machines.SleepClaim:
    """The claim a claimed `machine_sleep` job carries, as its step rebuilds it."""
    return machines.SleepClaim.from_wire(claim_at=kw["claim_at"], after_s=kw["after_s"], since=kw["since"])


async def _terminal(workspace_id: uuid.UUID, task_id: uuid.UUID) -> None:
    """`delete_task` wants a terminal task."""
    async with session_scope(workspace_id=workspace_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        task.status = "delivered"
        await s.commit()


async def _events(workspace_id: uuid.UUID, task_id: uuid.UUID, kind: str) -> list[dict[str, Any]]:
    async with session_scope(workspace_id=workspace_id) as s:
        rows = (await s.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id))).all()
        return [dict(r.payload) for r in rows if r.type == kind]


async def test_sleep_stores_the_checkpoint_destroys_the_frame_and_says_so(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    mid, tid, _, workspace = await _warm(fake_frames)
    try:
        assert await machines.sleep(mid, 1, SleepReason.IDLE) == "slept"
        row = await _row(workspace.id, mid)
        assert row is not None and row.state == MachineState.ASLEEP and row.frame_ref is None
        assert (
            row.checkpoint_object == f"machines/{mid}/1.tar.gz"
            and row.checkpoint_sha256
            and row.checkpoint_bytes
        )
        assert fake_frames.world == {}
        slept = await _events(workspace.id, tid, "machine.slept")
        assert slept and slept[0]["reason"] == "idle" and slept[0]["after_s"] is not None
    finally:
        await store.delete_prefix(machine_prefix(mid))


async def test_a_browser_machine_keeps_an_encrypted_profile(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    mid, _, _, workspace = await _warm(
        fake_frames,
        profile="browser",
        files={"/work/a": b"1", "/browser/profile/Default/Cookies": b"sid=SECRET"},
    )
    try:
        assert await machines.sleep(mid, 1, SleepReason.IDLE) == "slept"
        row = await _row(workspace.id, mid)
        assert row is not None and row.browser_object and row.browser_cipher_key_id
        blob = io.BytesIO()
        storage.get_file(row.browser_object, blob, settings.worker.snapshot_bucket)
        assert b"SECRET" not in blob.getvalue()
    finally:
        await store.delete_prefix(machine_prefix(mid))


async def test_a_browser_archive_that_fails_forgets_the_older_one(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed browser archive is a lost login, never an older one: the previous epoch's
    cookies may hold a session the user has since logged out of, and the next wake would restore it."""
    mid, _, _, workspace = await _warm(
        fake_frames,
        profile="browser",
        files={"/work/a": b"1", "/browser/profile/Default/Cookies": b"sid=OLD"},
    )
    try:
        older = f"machines/{mid}/0.browser.enc"
        await asyncio.to_thread(
            storage.put_file, older, io.BytesIO(b"older cookies"), settings.worker.snapshot_bucket
        )
        async with session_scope(workspace_id=workspace.id) as s:
            row = await s.get(Machine, mid)
            assert row is not None
            row.browser_object, row.browser_bytes, row.browser_cipher_key_id = older, 13, "k0"
            await s.commit()

        async def refused(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("the store refused the archive")

        monkeypatch.setattr(store, "save_browser", refused)
        assert await machines.sleep(mid, 1, SleepReason.IDLE) == "slept"
        row = await _row(workspace.id, mid)
        assert row is not None and row.state == MachineState.ASLEEP and row.checkpoint_object
        assert (row.browser_object, row.browser_bytes, row.browser_cipher_key_id) == (None, None, None)
        assert older not in await store.list_prefix(machine_prefix(mid)), "nothing names it now"
    finally:
        await store.delete_prefix(machine_prefix(mid))


async def test_over_the_cap_the_machine_is_gone_and_the_event_says_how_much(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.machines, "checkpoint_max_bytes", 64)
    mid, tid, _, workspace = await _warm(fake_frames, files={"/work/big": b"x" * 5000})
    assert await machines.sleep(mid, 1, SleepReason.IDLE) == "dropped"
    row = await _row(workspace.id, mid)
    assert row is not None and row.state == MachineState.GONE and row.gone_reason == "oversize"
    assert row.checkpoint_object is None
    assert (await _events(workspace.id, tid, "machine.slept"))[0]["dropped_bytes"] == 5000
    assert fake_frames.world == {}
    assert await store.list_prefix(machine_prefix(mid)) == []


async def test_a_stopped_frame_sleeps_with_its_unsaved_work(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """A restart stopped the frame with its files intact: the sleep's bind starts it, and the new
    checkpoint holds what `/work` holds now - not the older checkpoint - so nothing is lost."""
    mid, tid, _, workspace = await _warm(fake_frames, files={"/work/unsaved.md": b"since the last sleep"})
    await _set_checkpoint(workspace.id, mid)
    fake_frames.world[frame_name(mid, 1)].stop()
    try:
        assert await machines.sleep(mid, 1, SleepReason.IDLE) == "slept"
        row = await _row(workspace.id, mid)
        assert row is not None and row.state == MachineState.ASLEEP and row.frame_ref is None
        assert row.checkpoint_object == f"machines/{mid}/1.tar.gz"
        assert await _events(workspace.id, tid, "machine.rebuilt") == []
    finally:
        await store.delete_prefix(machine_prefix(mid))


async def test_a_sleep_whose_frame_vanished_says_its_unsaved_work_was_lost(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """The frame went before the sleep could save it, so what `/work`
    held since the last checkpoint is lost - the timeline says so after the plain `slept`, and the
    next wake does not say it again."""
    mid, tid, _, workspace = await _warm(fake_frames)
    fake_frames.world.clear()
    assert await machines.sleep(mid, 1, SleepReason.IDLE) == "slept"
    assert await _events(workspace.id, tid, "machine.slept")
    assert [e["reason"] for e in await _events(workspace.id, tid, "machine.rebuilt")] == ["lost"]
    row = await _row(workspace.id, mid)
    assert row is not None and row.state == MachineState.GONE and row.gone_reason is None


async def test_an_upload_failure_thaws_and_leaves_it_warm(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid, _, _, workspace = await _warm(fake_frames)

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("s3 is down")

    monkeypatch.setattr(store, "save_work", boom)
    assert await machines.sleep(mid, 1, SleepReason.IDLE) == "failed"
    row = await _row(workspace.id, mid)
    assert row is not None and row.state == MachineState.IDLE
    frame = next(iter(fake_frames.world.values()))
    assert frame.frozen is False


async def test_a_task_deleted_mid_sleep_leaves_no_frame_and_no_object(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row cascades away while the sleep holds a frame and writes objects."""
    mid, tid, _, workspace = await _warm(fake_frames)
    real = store.save_work

    async def save_then_delete(frame: Any, key: str, cap: int) -> Any:
        stored = await real(frame, key, cap)
        async with session_scope(workspace_id=workspace.id) as s:
            await s.execute(delete(Task).where(Task.id == tid))
            await s.commit()
        return stored

    monkeypatch.setattr(store, "save_work", save_then_delete)
    assert await machines.sleep(mid, 1, SleepReason.IDLE) == "gone"
    assert fake_frames.world == {}
    assert await store.list_prefix(machine_prefix(mid)) == []


async def test_a_task_deleted_before_the_upload_finishes_without_a_retry(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other order: the task is deleted and its `machine_forget` job destroys
    the frame the sleep holds before the upload, so the upload fails on a dead frame. The sleep
    must finish - no frame, no object, no raise for DBOS to record as an error."""
    mid, tid, ada, workspace = await _warm(fake_frames)
    await _terminal(workspace.id, tid)

    async def delete_then_fail(frame: Any, key: str, cap: int) -> Any:
        async with session_scope(workspace_id=workspace.id) as s:
            await task_ops.delete_task(s, ada, tid)
        await machines.forget(machines.Forgotten(mid, frame_name(mid, 1)))  # the forget job wins the race
        raise RuntimeError("the frame is gone")

    async def thaw_a_dead_frame(self: Any) -> None:
        raise RuntimeError("no such container")

    monkeypatch.setattr(store, "save_work", delete_then_fail)
    monkeypatch.setattr(fake_frames, "thaw", thaw_a_dead_frame)
    assert await machines.sleep(mid, 1, SleepReason.IDLE) == "gone"
    assert fake_frames.world == {}
    assert await store.list_prefix(machine_prefix(mid)) == []


async def test_a_second_sleep_at_the_same_epoch_is_skipped(fake_frames: Any, silent_dispatcher: None) -> None:
    mid, _, _, workspace = await _warm(fake_frames)
    try:
        assert await machines.sleep(mid, 1, SleepReason.IDLE) == "slept"
        assert await machines.sleep(mid, 1, SleepReason.IDLE) == "skipped"
        assert await machines.sleep(mid, 2, SleepReason.IDLE) == "skipped"
        row = await _row(workspace.id, mid)
        assert row is not None and row.state == MachineState.ASLEEP
    finally:
        await store.delete_prefix(machine_prefix(mid))


async def test_sleep_now_follows_the_role_and_state_table(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher
) -> None:
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
        cy, _ = await member(s, "Cy", role=WorkspaceRole.MEMBER, workspace=workspace)
        project = (await project_ops.create(s, ada, name=f"launch-{uuid.uuid4().hex[:6]}")).project
        s.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
        s.add(ProjectMember(project_id=project.id, actor_id=cy.id, role="viewer", added_by=ada.id))
        task = await task_ops.create_task(s, ada, goal="shared", project_id=project.id)
        row = Machine(
            workspace_id=workspace.id,
            task_id=task.id,
            driver="docker",
            profile="base",
            state=MachineState.RUNNING,
            state_since=utcnow(),
            last_active_at=utcnow(),
            epoch=2,
            frame_ref="machine-x-2",
        )
        s.add(row)
        await s.commit()
    async with session_scope(workspace_id=workspace.id) as s:
        with pytest.raises(Forbidden):
            await machines.request_sleep(s, cy, task.id)
        with pytest.raises(Conflict):
            await machines.request_sleep(s, bob, task.id)
    async with session_scope(workspace_id=workspace.id) as s:
        (await s.get(Machine, row.id)).state = MachineState.IDLE  # pyright: ignore[reportOptionalMemberAccess]
        await s.commit()
    async with session_scope(workspace_id=workspace.id) as s:
        slept = await machines.request_sleep(s, bob, task.id)
        assert slept.state == MachineState.SLEEPING
    assert [k for j, k in recording_dispatcher.enqueues if j == "machine_sleep"] == [
        f"machine-sleep-{row.id}-2~{slept.state_since.isoformat()}"
    ]
    async with session_scope(workspace_id=workspace.id) as s:  # a second click is a no-op, not an error
        assert (await machines.request_sleep(s, bob, task.id)).state == MachineState.SLEEPING
    assert len([e for e in recording_dispatcher.enqueues if e[0] == "machine_sleep"]) == 1


async def test_a_claimed_sleep_records_who_asked_and_how_long_it_was_idle(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid, tid, ada, workspace = await _warm(fake_frames, since_s=90)
    sent = _capture(recording_dispatcher, monkeypatch)
    try:
        async with session_scope(workspace_id=workspace.id) as s:
            assert (await machines.request_sleep(s, ada, tid)).state == MachineState.SLEEPING
        (job, _, kw) = sent[-1]
        assert job == "machine_sleep" and kw["claimed"] is True and kw["after_s"] >= 90
        # the job's second step, as the worker runs it
        done = await machines.sleep(
            mid,
            kw["epoch"],
            SleepReason(kw["reason"]),
            actor_id=uuid.UUID(kw["actor_id"]),
            claim=_claim_of(kw),
        )
        assert done == "slept"
        slept = await _events(workspace.id, tid, "machine.slept")
        assert slept and slept[0]["reason"] == "manual" and slept[0]["actor"]["name"] == "Ada"
        assert slept[0]["after_s"] == kw["after_s"]
    finally:
        await store.delete_prefix(machine_prefix(mid))


async def test_a_retried_sleep_now_gets_a_new_job_id(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T6-I1: a failed Sleep now (S3 down: back to warm) must not use up the id the next click
    needs - DBOS never re-runs a finished workflow id, and nothing else picks up a `sleeping` row."""
    mid, tid, ada, workspace = await _warm(fake_frames)

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("s3 is down")

    monkeypatch.setattr(store, "save_work", boom)
    sent = _capture(recording_dispatcher, monkeypatch)
    keys: list[str] = []
    for _ in range(2):
        async with session_scope(workspace_id=workspace.id) as s:
            assert (await machines.request_sleep(s, ada, tid)).state == MachineState.SLEEPING
        (_, key, kw) = sent[-1]
        keys.append(key)
        assert key == f"machine-sleep-{mid}-1~{kw['claim_at']}"  # the key IS the claim's identity
        assert (
            await machines.sleep(mid, 1, SleepReason.MANUAL, actor_id=ada.id, claim=_claim_of(kw)) == "failed"
        )
        assert (row := await _row(workspace.id, mid)) is not None and row.state == MachineState.IDLE
    assert keys[0] != keys[1]
    assert all(k.startswith(f"machine-sleep-{mid}-1~") for k in keys)


async def test_a_claim_survives_a_crash_before_the_slow_part(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """T6-I2: the job claims in one step and sleeps in the next, so a worker that dies between
    them resumes at the slow part, claimed - an unclaimed re-run would find `sleeping` and skip,
    stranding the row."""
    mid, tid, _, workspace = await _warm(
        fake_frames, state=MachineState.PARKED, held_by="step:x:1", since_s=90
    )
    try:
        claim = await machines.claim_sleep(mid, 1)
        assert claim is not None and claim.after_s >= 90
        assert (row := await _row(workspace.id, mid)) is not None and row.state == MachineState.SLEEPING
        assert await machines.claim_sleep(mid, 1) is None  # one claim per sleep
        assert await machines.sleep(mid, 1, SleepReason.PARK_HOLD) == "skipped"  # the unclaimed rule
        # ... the worker died here; DBOS resumes the workflow at its second step:
        done = await machines.sleep(mid, 1, SleepReason.PARK_HOLD, claim=claim)
        assert done == "slept"
        assert (row := await _row(workspace.id, mid)) is not None and row.state == MachineState.ASLEEP
        slept = await _events(workspace.id, tid, "machine.slept")
        assert slept and slept[0]["reason"] == "park_hold" and slept[0]["after_s"] == claim.after_s
    finally:
        await store.delete_prefix(machine_prefix(mid))


def _flaky_upload(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """`store.save_work` fails while the returned flag is up (S3 down), and works once it drops."""
    real, down = store.save_work, [True]

    async def flaky(frame: Any, key: str, cap: int) -> Any:
        if down[0]:
            raise RuntimeError("s3 is down")
        return await real(frame, key, cap)

    monkeypatch.setattr(store, "save_work", flaky)
    return down


async def test_two_claims_at_one_epoch_the_stale_one_changes_nothing(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T6-I3 (a): C1's step unclaims (its upload failed) and the worker dies before DBOS records
    the step; C2 claims at the same epoch; DBOS re-runs C1's step. It must see the row is C2's and
    touch nothing - and C2 then sleeps the Machine normally."""
    mid, _, _, workspace = await _warm(fake_frames)
    down = _flaky_upload(monkeypatch)
    try:
        c1 = await machines.claim_sleep(mid, 1)
        assert c1 is not None
        assert await machines.sleep(mid, 1, SleepReason.IDLE, claim=c1) == "failed"  # ... then the crash
        down[0] = False
        c2 = await machines.claim_sleep(mid, 1)
        assert c2 is not None and c2.at != c1.at
        assert await machines.sleep(mid, 1, SleepReason.IDLE, claim=c1) == "skipped"  # the re-run
        row = await _row(workspace.id, mid)
        assert row is not None and row.state == MachineState.SLEEPING and row.state_since == c2.at
        assert list(fake_frames.world) == [frame_name(mid, 1)]
        assert await store.list_prefix(machine_prefix(mid)) == []
        assert await machines.sleep(mid, 1, SleepReason.IDLE, claim=c2) == "slept"
        row = await _row(workspace.id, mid)
        assert row is not None and row.state == MachineState.ASLEEP and row.checkpoint_object
        assert await store.list_prefix(machine_prefix(mid)) == [row.checkpoint_object]
    finally:
        await store.delete_prefix(machine_prefix(mid))


async def _hand_back_and_claim(workspace_id: uuid.UUID, mid: uuid.UUID) -> machines.SleepClaim:
    """The row is handed back to warm under a running sleep (the stuck arm), and claimed again at
    the same epoch."""
    async with session_scope(workspace_id=workspace_id) as s:
        row = await s.get(Machine, mid)
        assert row is not None
        row.state = MachineState.IDLE
        await s.commit()
    claim = await machines.claim_sleep(mid, 1)
    assert claim is not None
    return claim


async def test_a_stale_finisher_never_deletes_the_object_the_row_references(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T6-I3 (b): C1 uploads, then loses the row: C2 claims and sleeps it at the same epoch,
    committing the same key. C1's finisher must not delete the object the row now points at."""
    mid, _, _, workspace = await _warm(fake_frames)
    real = store.save_work
    outcome: dict[str, str] = {}

    async def upload_then_lose_the_row(frame: Any, key: str, cap: int) -> Any:
        stored = await real(frame, key, cap)
        if not outcome:
            outcome["c2"] = "running"
            c2 = await _hand_back_and_claim(workspace.id, mid)
            outcome["c2"] = await machines.sleep(mid, 1, SleepReason.IDLE, claim=c2)
        return stored

    monkeypatch.setattr(store, "save_work", upload_then_lose_the_row)
    try:
        c1 = await machines.claim_sleep(mid, 1)
        assert c1 is not None
        assert await machines.sleep(mid, 1, SleepReason.IDLE, claim=c1) == "gone"
        assert outcome["c2"] == "slept"
        row = await _row(workspace.id, mid)
        assert row is not None and row.state == MachineState.ASLEEP and row.checkpoint_object
        assert await store.list_prefix(machine_prefix(mid)) == [row.checkpoint_object]
        assert fake_frames.world == {}
    finally:
        await store.delete_prefix(machine_prefix(mid))


async def test_a_stale_step_leaves_the_frame_and_keys_of_the_claim_that_took_the_row(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T6-I3: C1 uploads, then finds C2 holding the row (still `sleeping`, at the same epoch).
    It destroys nothing and deletes nothing - the frame and this epoch's key are C2's now."""
    mid, _, _, workspace = await _warm(fake_frames)
    real = store.save_work
    taken: list[machines.SleepClaim] = []

    async def upload_then_lose_the_row(frame: Any, key: str, cap: int) -> Any:
        stored = await real(frame, key, cap)
        if not taken:
            taken.append(await _hand_back_and_claim(workspace.id, mid))
        return stored

    monkeypatch.setattr(store, "save_work", upload_then_lose_the_row)
    try:
        c1 = await machines.claim_sleep(mid, 1)
        assert c1 is not None
        assert await machines.sleep(mid, 1, SleepReason.IDLE, claim=c1) == "skipped"
        row = await _row(workspace.id, mid)
        assert row is not None and row.state == MachineState.SLEEPING and row.state_since == taken[0].at
        assert list(fake_frames.world) == [frame_name(mid, 1)]  # C2's frame, untouched
        assert await store.list_prefix(machine_prefix(mid)) == [f"machines/{mid}/1.tar.gz"]
        assert await machines.sleep(mid, 1, SleepReason.IDLE, claim=taken[0]) == "slept"
        assert fake_frames.world == {}
    finally:
        await store.delete_prefix(machine_prefix(mid))


async def test_a_stale_step_whose_upload_fails_never_unclaims_the_claim_that_took_the_row(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T6-I3, `_unclaim`: C1's upload fails after C2 took the row. C1 must not hand C2's claim
    back to warm (with C1's stale clock), nor thaw or destroy the frame."""
    mid, _, _, workspace = await _warm(fake_frames)
    taken: list[machines.SleepClaim] = []

    async def lose_the_row_then_fail(frame: Any, key: str, cap: int) -> Any:
        taken.append(await _hand_back_and_claim(workspace.id, mid))
        raise RuntimeError("s3 is down")

    monkeypatch.setattr(store, "save_work", lose_the_row_then_fail)
    c1 = await machines.claim_sleep(mid, 1)
    assert c1 is not None
    assert await machines.sleep(mid, 1, SleepReason.IDLE, claim=c1) == "skipped"
    row = await _row(workspace.id, mid)
    assert row is not None and row.state == MachineState.SLEEPING and row.state_since == taken[0].at
    assert list(fake_frames.world) == [frame_name(mid, 1)]


async def test_a_frame_that_cannot_be_made_unclaims_with_its_clock(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Minor 4: any failure before the frame is known unclaims - parked, with the hold clock where
    it was, so the next sweep retries instead of the reconciler finding a stuck `sleeping` row."""
    mid, _, _, workspace = await _warm(fake_frames, state=MachineState.PARKED, held_by="step:x:1", since_s=90)
    parked = await _row(workspace.id, mid)
    assert parked is not None

    def no_driver(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no frame driver")

    monkeypatch.setattr(frames, "new_frame", no_driver)
    assert await machines.sleep(mid, 1, SleepReason.PARK_HOLD) == "failed"
    row = await _row(workspace.id, mid)
    assert row is not None and row.state == MachineState.PARKED and row.state_since == parked.state_since
    assert list(fake_frames.world) == [frame_name(mid, 1)]


async def test_reset_bumps_the_epoch_clears_the_objects_and_names_who(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher
) -> None:
    mid, tid, ada, workspace = await _warm(fake_frames)
    async with session_scope(workspace_id=workspace.id) as s:
        row = await machines.reset(s, ada, tid)
        assert row.state == MachineState.GONE and row.epoch == 2 and row.frame_ref is None
        assert row.gone_reason is None
    assert ("machine_reset", f"machine-reset-{mid}-2") in recording_dispatcher.enqueues
    reset = await _events(workspace.id, tid, "machine.reset")
    assert reset and reset[0]["actor"]["name"] == "Ada"


async def test_the_reset_job_destroys_the_old_frame_and_deletes_the_old_objects(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    mid, tid, ada, workspace = await _warm(fake_frames)
    await machines.sleep(mid, 1, SleepReason.IDLE)
    assert await store.list_prefix(machine_prefix(mid)) != []
    async with session_scope(workspace_id=workspace.id) as s:
        row = await machines.reset(s, ada, tid)
        assert row.state == MachineState.GONE and not row.has_objects
    stray = fake_frames(str(tid), name=frame_name(mid, 1))
    await stray.create()  # a frame the reconciler has not caught yet
    await machines.cleanup(frame_name(mid, 1), [f"machines/{mid}/1.tar.gz"])
    assert fake_frames.world == {}
    assert await store.list_prefix(machine_prefix(mid)) == []


async def test_expire_deletes_the_objects_and_keeps_the_reason(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    mid, _, _, workspace = await _warm(fake_frames)
    await machines.sleep(mid, 1, SleepReason.IDLE)
    assert await machines.expire(mid, 1) == "expired"
    row = await _row(workspace.id, mid)
    assert row is not None and row.state == MachineState.GONE and row.gone_reason == "expired"
    assert row.checkpoint_object is None and await store.list_prefix(machine_prefix(mid)) == []
    assert await machines.expire(mid, 0) == "skipped"


async def test_deleting_a_task_enqueues_forget_and_never_touches_the_frame(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 5: the API process never touches a frame - `delete_task` reads the Machine before the
    cascade and hands it to the worker's `machine_forget` job after the commit."""
    mid, tid, ada, workspace = await _warm(fake_frames)
    await _terminal(workspace.id, tid)
    sent = _capture(recording_dispatcher, monkeypatch)
    async with session_scope(workspace_id=workspace.id) as s:
        await task_ops.delete_task(s, ada, tid)
    forgets = [(key, kw) for job, key, kw in sent if job == "machine_forget"]
    assert forgets == [
        (
            f"machine-forget-{mid}",
            {"machine_id": str(mid), "frame_ref": frame_name(mid, 1), "driver": "docker"},
        )
    ]
    assert await _row(workspace.id, mid) is None
    assert list(fake_frames.world) == [frame_name(mid, 1)]  # still there: the request left it alone
    await machines.forget(machines.Forgotten(mid, frame_name(mid, 1)))  # the job's body, on the worker
    assert fake_frames.world == {}


async def test_forget_deletes_a_deleted_tasks_objects(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher
) -> None:
    mid, tid, ada, workspace = await _warm(fake_frames)
    try:
        await machines.sleep(mid, 1, SleepReason.IDLE)
        await _terminal(workspace.id, tid)
        async with session_scope(workspace_id=workspace.id) as s:
            await task_ops.delete_task(s, ada, tid)
        assert ("machine_forget", f"machine-forget-{mid}") in recording_dispatcher.enqueues
        assert await store.list_prefix(machine_prefix(mid)) != []  # the request deleted nothing
        await machines.forget(machines.Forgotten(mid, None))
        assert await store.list_prefix(machine_prefix(mid)) == []
    finally:
        await store.delete_prefix(machine_prefix(mid))


async def _set_driver(workspace_id: uuid.UUID, machine_id: uuid.UUID, driver: str) -> None:
    async with session_scope(workspace_id=workspace_id) as s:
        row = await s.get(Machine, machine_id)
        assert row is not None
        row.driver = driver
        await s.commit()


async def test_a_sleep_reaches_the_frame_through_the_rows_driver(fake_frames: Any) -> None:
    mid, _, _, workspace = await _warm(fake_frames)
    await _set_driver(workspace.id, mid, "sandbar")
    fake_frames.made.clear()
    assert await machines.sleep(mid, 1, SleepReason.IDLE) == "slept"
    assert fake_frames.made == [("sandbar", frame_name(mid, 1))]


async def test_reset_hands_its_cleanup_the_driver_that_holds_the_old_frame(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reset job names the old frame's driver; a job enqueued before
    that field existed carries none and cleans up on this worker's driver, as it always did."""
    sent = _capture(recording_dispatcher, monkeypatch)
    mid, tid, ada, workspace = await _warm(fake_frames)
    await _set_driver(workspace.id, mid, "sandbar")
    async with session_scope(workspace_id=workspace.id) as s:
        await machines.reset(s, ada, tid)
    ((_, _, kw),) = [entry for entry in sent if entry[0] == "machine_reset"]
    assert kw["frame_ref"] == frame_name(mid, 1) and kw["driver"] == "sandbar"
    asked: list[tuple[str, str | None]] = []

    async def destroy_ref(ref: str, *, driver: str | None = None) -> None:
        asked.append((ref, driver))

    monkeypatch.setattr(frames, "destroy_ref", destroy_ref)
    await machines.cleanup(kw["frame_ref"], [], kw["driver"])
    await machines.cleanup(kw["frame_ref"], [])  # a job from before the field
    assert asked == [(frame_name(mid, 1), "sandbar"), (frame_name(mid, 1), None)]


async def test_a_forgotten_machine_names_its_driver(
    fake_frames: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid, tid, _, workspace = await _warm(fake_frames)
    await _set_driver(workspace.id, mid, "sandbar")
    async with session_scope(workspace_id=workspace.id) as s:
        found = await machines.forgettable(s, tid)
    assert found is not None
    assert found == machines.Forgotten(mid, frame_name(mid, 1), "sandbar")
    asked: list[tuple[str, str | None]] = []

    async def destroy_ref(ref: str, *, driver: str | None = None) -> None:
        asked.append((ref, driver))

    monkeypatch.setattr(frames, "destroy_ref", destroy_ref)
    await machines.forget(found)
    assert asked == [(frame_name(mid, 1), "sandbar")]


async def test_a_native_machine_sleeps_suspended_and_keeps_its_frame(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """A native driver's Machine is archived first (`/work` is always the
    portable copy), then thawed and suspended; the row keeps naming the frame - natively asleep."""
    mid, _, _, workspace = await _warm(fake_frames, driver=NATIVE)
    try:
        assert await machines.sleep(mid, 1, SleepReason.IDLE) == "slept"
        row = await _row(workspace.id, mid)
        assert row is not None and row.state == MachineState.ASLEEP and row.checkpoint_object
        assert row.frame_ref == frame_name(mid, 1), "natively asleep: the row still names its frame"
        live = fake_frames.world[frame_name(mid, 1)]
        assert live.suspended
        assert live.calls[-2:] == ["thaw", "suspend"], "thawed, then suspended"
    finally:
        await store.delete_prefix(machine_prefix(mid))


async def test_a_native_suspend_that_fails_is_a_failed_sleep_and_the_frame_stays_awake(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid, _, _, workspace = await _warm(fake_frames, driver=NATIVE)

    async def refuse(self: Any) -> None:
        raise RuntimeError("the host would not suspend it")

    monkeypatch.setattr(NativeFakeFrame, "suspend", refuse)
    try:
        assert await machines.sleep(mid, 1, SleepReason.IDLE) == "failed"
        row = await _row(workspace.id, mid)
        assert row is not None and row.state == MachineState.IDLE and row.frame_ref == frame_name(mid, 1)
        live = fake_frames.world[frame_name(mid, 1)]
        assert not live.suspended and "suspend" not in live.calls
        assert live.calls[-1] == "thaw", "frozen for the archive, then thawed again"
    finally:
        await store.delete_prefix(machine_prefix(mid))


async def test_a_native_machine_over_the_cap_sleeps_the_archive_way(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A native driver's Machine whose `/work` was not stored (over the cap) has no
    portable copy to wake from, so it is never kept suspended - it goes the archive way: the frame
    is destroyed and the row names none."""
    monkeypatch.setattr(settings.machines, "checkpoint_max_bytes", 64)
    mid, _, _, workspace = await _warm(fake_frames, driver=NATIVE, files={"/work/big": b"x" * 5000})
    assert await machines.sleep(mid, 1, SleepReason.IDLE) == "dropped"
    row = await _row(workspace.id, mid)
    assert row is not None and row.state == MachineState.GONE and row.gone_reason == "oversize"
    assert row.frame_ref is None, "nothing to wake from: the row names no frame"
    assert fake_frames.world == {}, "destroyed, never suspended"
    assert await store.list_prefix(machine_prefix(mid)) == []


async def _machine_usage(workspace_id: uuid.UUID, task_id: uuid.UUID) -> list[UsageEvent]:
    async with session_scope(workspace_id=workspace_id) as s:
        rows = list(
            (
                await s.scalars(
                    select(UsageEvent).where(UsageEvent.task_id == task_id, UsageEvent.family == "machine")
                )
            ).all()
        )
        s.expunge_all()  # the scope's closing rollback would expire them
        return rows


async def test_a_native_suspend_closes_the_warm_segment_once(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """A suspended computer is not warm time - the sleep bills the idle tail on
    the row's driver and stops the meter, once; a replayed sleep bills nothing more."""
    mid, tid, _, workspace = await _warm(fake_frames, driver=NATIVE)
    async with session_scope(workspace_id=workspace.id) as s:
        row = await s.get(Machine, mid)
        assert row is not None
        row.metered_until = await Machine.db_now(s) - dt.timedelta(seconds=600)  # ten idle minutes
        await s.commit()
    try:
        assert await machines.sleep(mid, 1, SleepReason.IDLE) == "slept"
        row = await _row(workspace.id, mid)
        assert row is not None and row.state == MachineState.ASLEEP and row.frame_ref == frame_name(mid, 1)
        assert row.metered_until is None, "the meter stops while it sleeps"
        assert await machines.sleep(mid, 1, SleepReason.IDLE) == "skipped"
        (usage,) = await _machine_usage(workspace.id, tid)
        assert 600 <= usage.units["second"] < 900 and usage.provider == NATIVE
    finally:
        await store.delete_prefix(machine_prefix(mid))
