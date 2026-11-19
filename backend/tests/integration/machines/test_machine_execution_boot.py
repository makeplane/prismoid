# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A Machine booted early: the first step finds it warm or waits for it, a boot never
takes what a step made or evicts for a guess, and the prewarm boots only where it cannot guess wrong.
Which turns boot at all (computer work, never talk or text) is `test_machine_on_need.py`.
The rig is `tests/support/rigs/machine_execution_rig.py`."""

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from prismoid.ai import frames
from prismoid.conf import settings
from prismoid.db.models.machine import Machine
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.operations import early_boot, machines
from prismoid.schemas.machines import MachineProfile, MachineState, boot_holder
from tests.support.rigs.machine_execution_rig import canned as canned
from tests.support.rigs.machine_execution_rig import model as model
from tests.support.rigs.machine_execution_rig import step_machine, step_run, task_with_steps

pytestmark = pytest.mark.integration


async def _boot_row(task_id: uuid.UUID, state: MachineState, *, held_by: str | None) -> None:
    """A Machine row as a pass of the lifecycle would have left it, with no frame."""
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        now = await s.scalar(text("select now()"))
        s.add(
            Machine(
                workspace_id=task.workspace_id,
                task_id=task_id,
                driver=settings.worker.frame_driver,
                profile=MachineProfile.BROWSER,
                state=state,
                state_since=now,
                last_active_at=now,
                epoch=1,
                held_by=held_by,
                held_since=now if held_by else None,
                egress_version=0,
                egress_hosts=[],
            )
        )
        await s.commit()


async def test_a_booted_machine_is_bound_warm_by_the_first_step(
    fake_frames: Any, silent_dispatcher: None, canned: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.computer_use, "enabled", True)
    task_id, (first,) = await task_with_steps(1)
    assert await machines.boot(task_id) == "booted"
    row = await step_machine(task_id)
    assert row is not None and row.state == MachineState.IDLE and row.held_by is None
    assert row.profile == MachineProfile.BROWSER, "the widest profile: every step binds it"
    assert row.metered_until is not None, "its warm time is the task's from the boot on"
    assert fake_frames.world[row.frame_ref].egress_hosts == [], "an idle Machine reaches nothing"

    await step_run(task_id, first)
    assert len(fake_frames.created) == 1, "the step bound the booted frame instead of starting one"
    after = await step_machine(task_id)
    assert after is not None and after.epoch == 1 and after.state == MachineState.IDLE


async def test_a_step_that_arrives_mid_boot_waits_for_it(
    fake_frames: Any, silent_dispatcher: None, canned: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, (first,) = await task_with_steps(1)
    create = fake_frames.create
    started = asyncio.Event()

    async def slow_create(self: Any, *args: Any, **kwargs: Any) -> None:
        started.set()
        await asyncio.sleep(1.5)  # the boot is still `waking` when the step claims
        await create(self, *args, **kwargs)

    from prismoid.operations.machines import lifecycle

    claim = getattr(lifecycle, "_claim")  # noqa: B009 - spied on, to see what the step was told
    told: list[object] = []

    async def spy(*args: Any, **kwargs: Any) -> object:
        told.append(outcome := await claim(*args, **kwargs))
        return outcome

    monkeypatch.setattr(fake_frames, "create", slow_create)
    monkeypatch.setattr(lifecycle, "_claim", spy)
    booting = asyncio.create_task(machines.boot(task_id))
    await started.wait()
    await step_run(task_id, first)
    assert await booting == "booted"
    assert len(fake_frames.created) == 1, "one frame: the step waited for the boot's"
    assert "the Machine is starting" in told, "the step knew the boot was starting it, not busy"
    row = await step_machine(task_id)
    assert row is not None and row.epoch == 1


async def test_with_computer_use_off_the_boot_asks_for_no_browser(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.computer_use, "enabled", False)
    task_id, _ = await task_with_steps(1)
    assert await machines.boot(task_id) == "booted"
    row = await step_machine(task_id)
    assert row is not None and row.profile == MachineProfile.BASE


async def test_a_boot_never_takes_a_machine_a_step_made(
    fake_frames: Any, silent_dispatcher: None, canned: list[str]
) -> None:
    task_id, (first,) = await task_with_steps(1)
    await step_run(task_id, first)
    assert await machines.boot(task_id) == "made"
    assert len(fake_frames.created) == 1


async def test_a_boot_never_evicts_for_a_guess(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.machines, "workspace_warm_cap", 1)
    task_id, _ = await task_with_steps(1)
    async with row_scope(Task, task_id) as s:  # the workspace's one warm slot, another task's
        task = await s.get(Task, task_id)
        assert task is not None
        other = Task(
            id=uuid.uuid4(),
            goal="the other task",
            creator_actor_id=task.creator_actor_id,
            criteria={},
            inputs=[],
            mode="standard",
        )
        s.add(other)
        await s.flush()
        await s.commit()
    await _boot_row(other.id, MachineState.IDLE, held_by=None)
    assert await machines.boot(task_id) == "full"
    assert await step_machine(task_id) is None
    warm = await step_machine(other.id)
    assert warm is not None and warm.state == MachineState.IDLE, "the warm Machine was not put to sleep"


async def test_a_boot_that_fails_leaves_the_first_step_to_start_it(
    fake_frames: Any, silent_dispatcher: None, canned: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, (first,) = await task_with_steps(1)
    create = fake_frames.create

    async def refused(self: Any, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("no capacity")

    monkeypatch.setattr(fake_frames, "create", refused)
    assert await machines.boot(task_id) == "failed"
    row = await step_machine(task_id)
    assert row is not None and row.state == MachineState.GONE and row.held_by is None

    monkeypatch.setattr(fake_frames, "create", create)
    await step_run(task_id, first)
    assert len(fake_frames.created) == 1
    after = await step_machine(task_id)
    assert after is not None and after.state == MachineState.IDLE and after.epoch == 2


async def test_a_step_takes_over_a_dead_boots_hold(
    fake_frames: Any, silent_dispatcher: None, canned: list[str]
) -> None:
    """A boot whose worker died is put to rest by the stuck rule with its hold kept (as a step's
    attempt is); the next step must not wait on a hold nobody will let go of."""
    task_id, (first,) = await task_with_steps(1)
    await _boot_row(task_id, MachineState.GONE, held_by=boot_holder(task_id))
    await step_run(task_id, first)
    row = await step_machine(task_id)
    assert row is not None and row.state == MachineState.IDLE and row.held_by is None and row.epoch == 2


@pytest.mark.parametrize(
    ("driver", "enabled", "booted"),
    [
        ("remote", True, True),  # every profile there is one template's computer
        ("remote", False, False),  # PRISMOID_MACHINES_BOOT_ON_ACCEPT=false
        ("docker", True, False),  # a profile there is its own image: the first step starts it
        ("sandbar", True, False),  # and a sandbox born with no list is airtight for good
        ("device", True, False),  # and a person's folder waits for the first step's checks
    ],
)
async def test_the_prewarm_boots_only_where_it_cannot_guess_wrong(
    recording_dispatcher: Any, monkeypatch: pytest.MonkeyPatch, driver: str, enabled: bool, booted: bool
) -> None:
    """`remote` stands for a driver that boots early (`frames.EARLY_BOOT_DRIVERS`); none of the
    shipped ones does."""
    monkeypatch.setattr(frames, "EARLY_BOOT_DRIVERS", frozenset({"remote"}))
    monkeypatch.setattr(settings.worker, "frame_driver", driver)
    monkeypatch.setattr(settings.machines, "boot_on_accept", enabled)
    task_id, _ = await task_with_steps(1)  # a committed coder step: it may need the computer
    async with row_scope(Task, task_id) as session:
        assert await early_boot.for_plan(session, task_id) is booted
    expected = [("machine_boot", f"machine-boot-{task_id}")] if booted else []
    assert [e for e in recording_dispatcher.enqueues if e[0] == "machine_boot"] == expected


async def test_a_boot_that_cannot_be_enqueued_never_fails_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    async def down(task_id: uuid.UUID, turn: int = 1) -> None:
        raise ConnectionError("the system database is away")

    monkeypatch.setattr(frames, "EARLY_BOOT_DRIVERS", frozenset({"remote"}))
    monkeypatch.setattr(settings.worker, "frame_driver", "remote")
    monkeypatch.setattr(machines, "boot_early", down)
    task_id, _ = await task_with_steps(1)
    async with row_scope(Task, task_id) as session:
        assert await early_boot.for_plan(session, task_id) is False
