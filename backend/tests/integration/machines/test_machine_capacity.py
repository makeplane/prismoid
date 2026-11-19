# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What keeps a host from filling up (ADR-076): the asleep cap's release, and the unused boot."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from prismoid import storage
from prismoid.conf import settings
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.models.usage import UsageEvent
from prismoid.db.session import row_scope, session_scope
from prismoid.errors import HostFull, ModelIncapable
from prismoid.operations import machines, verification
from prismoid.operations import tasks as task_ops
from prismoid.operations.execution.taps import step_event
from prismoid.operations.machines import wake_wait
from prismoid.operations.machines.common import record_after
from prismoid.schemas.events import MachineControlClaimed
from prismoid.schemas.machines import MachineState, SleepReason
from prismoid.schemas.plan import StepState
from prismoid.schemas.verification import Verdict
from tests.support.cast import member
from tests.support.fakes.fake_frames import DEVICE, NATIVE, FakeFrame

pytestmark = pytest.mark.integration


async def task_and_step() -> tuple[Any, uuid.UUID, uuid.UUID]:
    async with session_scope() as s:
        actor, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, actor, goal="machine work")
        step = PlanStep(
            id=uuid.uuid4(), task_id=task.id, ordinal=0, kind="step", state="ready", title="work", spec={}
        )
        s.add(step)
        await s.commit()
        return workspace, task.id, step.id


async def machine_of(task_id: uuid.UUID) -> Machine:
    async with row_scope(Task, task_id) as session:
        row = await Machine.for_task(session, task_id)
        assert row is not None
        session.expunge(row)
        return row


async def booted(task_id: uuid.UUID) -> Machine:
    assert await machines.boot(task_id) == "booted"
    return await machine_of(task_id)


@pytest.fixture
def computer_use(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.computer_use, "enabled", True)


async def test_a_boot_no_step_used_goes_unbilled_and_holds_nothing(
    fake_frames: Any, silent_dispatcher: None, computer_use: None
) -> None:
    """The clarify nobody answered, the task cancelled before its first step."""
    _, task_id, _ = await task_and_step()
    row = await booted(task_id)
    assert await machines.retire_or_sleep(row.id, row.epoch, SleepReason.IDLE) == "unused"
    after = await machine_of(task_id)
    assert after.state == MachineState.GONE and after.frame_ref is None and after.metered_until is None
    assert row.frame_ref not in fake_frames.world, "its slot on the host is free"
    async with row_scope(Task, task_id) as session:
        billed = await session.scalars(select(UsageEvent).where(UsageEvent.task_id == task_id))
        assert list(billed) == []


async def test_a_boot_someone_drove_is_a_machine_and_sleeps(
    fake_frames: Any, silent_dispatcher: None, computer_use: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a driver left before the first step is on this computer alone."""
    monkeypatch.setattr(settings.worker, "frame_driver", NATIVE)
    _, task_id, _ = await task_and_step()
    row = await booted(task_id)
    await record_after(task_id, MachineControlClaimed, actor=None, private=False)
    assert await machines.retire_or_sleep(row.id, row.epoch, SleepReason.IDLE) == "slept"
    after = await machine_of(task_id)
    assert after.state == MachineState.ASLEEP and after.has_objects


async def test_a_boot_a_step_started_on_sleeps_as_any_machine_does(
    fake_frames: Any, silent_dispatcher: None, computer_use: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.worker, "frame_driver", NATIVE)
    _, task_id, step_id = await task_and_step()
    row = await booted(task_id)
    async with row_scope(Task, task_id) as session:
        step = await session.get(PlanStep, step_id)
        assert step is not None
        step.attempt = 1
        await session.commit()
    assert await machines.retire_or_sleep(row.id, row.epoch, SleepReason.IDLE) == "slept"
    after = await machine_of(task_id)
    assert after.state == MachineState.ASLEEP and after.frame_ref == row.frame_ref


async def replanned(task_id: uuid.UUID) -> None:
    """A replan: every unfinished step dropped, attempts and all, and one nothing has started."""
    async with row_scope(Task, task_id) as session:
        await PlanStep.delete_unfinished(session, task_id)
        fresh = PlanStep(
            id=uuid.uuid4(), task_id=task_id, ordinal=0, kind="step", state="ready", title="again", spec={}
        )
        session.add(fresh)
        await session.commit()


async def step_ran(task_id: uuid.UUID, step_id: uuid.UUID) -> None:
    """What the ladder commits at an attempt's start: the attempt, and its `running` frame."""
    async with row_scope(Task, task_id) as session:
        task, step = await session.get(Task, task_id), await session.get(PlanStep, step_id)
        assert task is not None and step is not None
        step.attempt = 1
        await step_event(session, task, step, StepState.RUNNING)
        await session.commit()


async def test_a_boot_a_step_used_is_not_unused_once_a_replan_drops_that_step(
    fake_frames: Any, silent_dispatcher: None, computer_use: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replan parks on a clarify and the idle clock runs out: every step left is at attempt 0,
    and the work in the frame is still the step's. It sleeps, checkpoint and all."""
    monkeypatch.setattr(settings.worker, "frame_driver", NATIVE)
    _, task_id, step_id = await task_and_step()
    row = await booted(task_id)
    await step_ran(task_id, step_id)
    await replanned(task_id)
    assert await machines.retire_or_sleep(row.id, row.epoch, SleepReason.IDLE) == "slept"
    after = await machine_of(task_id)
    assert after.state == MachineState.ASLEEP and after.frame_ref == row.frame_ref


async def test_a_replan_of_steps_that_never_ran_leaves_the_boot_unused(
    fake_frames: Any, silent_dispatcher: None, computer_use: None
) -> None:
    _, task_id, _ = await task_and_step()
    row = await booted(task_id)
    await replanned(task_id)
    assert await machines.retire_or_sleep(row.id, row.epoch, SleepReason.IDLE) == "unused"


async def test_a_persistent_frame_is_never_retired_as_an_unused_boot(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A device frame is the task's work: it goes only with Reset or its task, whatever the steps
    left on the plan say."""
    monkeypatch.setattr(settings.worker, "frame_driver", DEVICE)
    workspace, task_id, step_id = await task_and_step()
    async with session_scope(workspace_id=workspace.id) as session:
        task, step = await session.get(Task, task_id), await session.get(PlanStep, step_id)
        assert task is not None and step is not None
        step.attempt = 1
        first = await machines.acquire(session, task, step, profile="base", allow_domains=[])
    await first.frame.write_bytes("/work/notes.md", b"the task's own file")
    await machines.release(first, parked=False)
    await replanned(task_id)
    row = await machine_of(task_id)
    assert await machines.retire_or_sleep(row.id, row.epoch, SleepReason.IDLE) == "slept"
    after = await machine_of(task_id)
    assert after.state == MachineState.ASLEEP and after.frame_ref == first.frame.ref
    assert b"the task's own file" in fake_frames.world[first.frame.ref].files["/work/notes.md"]


async def test_a_released_machine_gives_up_its_computer_and_keeps_its_checkpoint(
    fake_frames: Any, silent_dispatcher: None, computer_use: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.worker, "frame_driver", NATIVE)
    workspace, task_id, step_id = await task_and_step()
    row = await booted(task_id)
    async with row_scope(Task, task_id) as session:
        step = await session.get(PlanStep, step_id)
        assert step is not None
        step.attempt = 1
        await session.commit()
    await machines.sleep(row.id, row.epoch, SleepReason.IDLE)
    asleep = await machine_of(task_id)
    assert await machines.release_asleep(asleep.id, asleep.epoch - 1) == "skipped"
    assert await machines.release_asleep(asleep.id, asleep.epoch) == "released"
    released = await machine_of(task_id)
    assert released.state == MachineState.ASLEEP and released.frame_ref is None
    assert released.has_checkpoint, "the work stays: only the computer went"
    assert row.frame_ref not in fake_frames.world, "the room on the host is what this was for"
    assert await machines.release_asleep(asleep.id, asleep.epoch) == "skipped"
    async with session_scope(workspace_id=workspace.id) as session:
        task, step = await session.get(Task, task_id), await session.get(PlanStep, step_id)
        assert task is not None and step is not None
        step.attempt = 2
        woken = await machines.acquire(session, task, step, profile="base", allow_domains=[])
    assert woken.frame.ref != row.frame_ref, "the next wake rebuilds on a new computer"


async def natively_asleep(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, uuid.UUID, uuid.UUID, Machine]:
    monkeypatch.setattr(settings.worker, "frame_driver", NATIVE)
    workspace, task_id, step_id = await task_and_step()
    row = await booted(task_id)
    async with row_scope(Task, task_id) as session:
        step = await session.get(PlanStep, step_id)
        assert step is not None
        step.attempt = 1
        await session.commit()
    await machines.sleep(row.id, row.epoch, SleepReason.IDLE)
    return workspace, task_id, step_id, await machine_of(task_id)


async def released(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, uuid.UUID, uuid.UUID, Machine]:
    workspace, task_id, step_id, asleep = await natively_asleep(monkeypatch)
    assert await machines.release_asleep(asleep.id, asleep.epoch) == "released"
    return workspace, task_id, step_id, asleep


def full_for(monkeypatch: pytest.MonkeyPatch, times: float) -> dict[str, int]:
    """A host refusing the next `times` creates."""
    tries = {"n": 0}
    create = FakeFrame.create

    async def refusing(self: Any, *args: Any, **kwargs: Any) -> Any:
        tries["n"] += 1
        if tries["n"] <= times:
            raise HostFull("quota_exceeded at create: every host is full")
        return await create(self, *args, **kwargs)

    monkeypatch.setattr(FakeFrame, "create", refusing)
    monkeypatch.setattr(wake_wait, "RETRY_FIRST_S", 0.01)
    return tries


async def test_a_released_machine_waits_for_room_as_a_wake_does(
    fake_frames: Any, silent_dispatcher: None, computer_use: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, task_id, step_id, _ = await released(monkeypatch)
    tries = full_for(monkeypatch, 2)
    monkeypatch.setattr(settings.machines, "wake_retry_s", 30)
    async with session_scope(workspace_id=workspace.id) as session:
        task, step = await session.get(Task, task_id), await session.get(PlanStep, step_id)
        assert task is not None and step is not None
        step.attempt = 2
        await machines.acquire(session, task, step, profile="base", allow_domains=[])
    assert tries["n"] == 3
    assert (await machine_of(task_id)).state == MachineState.RUNNING


async def test_a_rebuild_a_wake_fell_to_tells_the_step_the_host_is_full(
    fake_frames: Any, silent_dispatcher: None, computer_use: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rebuild after "gone" does not say the wake's phase twice, and still says it is waiting."""
    workspace, task_id, step_id, asleep = await natively_asleep(monkeypatch)
    assert asleep.frame_ref is not None
    del fake_frames.world[asleep.frame_ref]
    tries = full_for(monkeypatch, 1)
    monkeypatch.setattr(settings.machines, "wake_retry_s", 30)
    phases: list[str] = []

    async def on_phase(label: str) -> None:
        phases.append(label)

    async with session_scope(workspace_id=workspace.id) as session:
        task, step = await session.get(Task, task_id), await session.get(PlanStep, step_id)
        assert task is not None and step is not None
        step.attempt = 2
        await machines.acquire(session, task, step, profile="base", allow_domains=[], on_phase=on_phase)
    assert tries["n"] == 2
    assert phases == ["Waking the Machine", wake_wait.WAKE_SLOW]


async def test_a_host_that_stays_full_is_one_infra_attempt_and_the_checkpoint_stays(
    fake_frames: Any, silent_dispatcher: None, computer_use: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, task_id, step_id, _ = await released(monkeypatch)
    full_for(monkeypatch, float("inf"))
    monkeypatch.setattr(settings.machines, "wake_retry_s", 0)
    async with session_scope(workspace_id=workspace.id) as session:
        task, step = await session.get(Task, task_id), await session.get(PlanStep, step_id)
        assert task is not None and step is not None
        step.attempt = 2
        with pytest.raises(ModelIncapable) as failed:
            await machines.acquire(session, task, step, profile="base", allow_domains=[])
    assert failed.value.context.get("infra")
    after = await machine_of(task_id)
    assert after.state == MachineState.ASLEEP and after.has_checkpoint


async def test_a_receipt_no_frame_ever_came_for_is_unverified_whatever_a_judge_would_say(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`final_verdict` lets a judge's PASS stand when a suite had nothing to examine; this one never
    looked, so the judge must not be asked."""
    _, task_id, step_id = await task_and_step()
    body = b"# memo\n"
    digest = storage.put(body)
    async with row_scope(Task, task_id) as session:
        artifact = Artifact(
            id=uuid.uuid4(),
            lineage_id=uuid.uuid4(),
            task_id=task_id,
            step_id=step_id,
            type="document",
            title="Memo",
            sha256=digest,
            bytes=len(body),
            storage_key=storage.key_for(digest),
            provenance={
                "path": "/artifacts/memo.md",
                "type": "document",
                "title": "Memo",
                "sha256": digest,
                "bytes": len(body),
                "sources": [],
                "agent": "maker",
                "model": "",
                "skill_pins": [],
                "at": "2026-11-01T00:00:00Z",
            },
        )
        session.add(artifact)
        await session.commit()

    class Full:
        async def create(self, **_kw: Any) -> None:
            raise HostFull("quota_exceeded at create: every host is full")

    async def judged(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("a judge read a receipt no check looked at")

    monkeypatch.setattr(verification, "new_frame", lambda **_kw: Full())
    monkeypatch.setattr(verification, "ensure_ready", lambda profile="base", driver=None: None)
    monkeypatch.setattr(verification, "judge", judged)
    async with row_scope(Task, task_id) as session:
        row = await session.get(Artifact, artifact.id)
        assert row is not None
        signed = await verification.verify_artifact(session, row, frame_wait_s=0)
    assert signed.report.verdict is Verdict.UNVERIFIED
    assert [c.name for c in signed.report.checks] == ["suite:frame"]
