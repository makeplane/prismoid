# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A Machine is prewarmed only for a committed plan whose work may need the computer (ADR-070, amended).

On a remote driver every booted Machine holds a host slot. A step takes the Machine at its first call that
needs it, and the prewarm starts it once a plan commits with such a step, never on a guess about
the words of an ask at the pick-up. A talk turn, a plan of text steps or a plan of seats boots
nothing, and `task.planned` says what the turn may ask of the Machine and which agents decided it.
The kill switch still stops every boot.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from prismoid.ai import frames
from prismoid.conf import settings
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.machine import Machine
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import early_boot, machines, planning
from prismoid.operations import tasks as task_ops
from prismoid.schemas.events import TaskPlanned
from prismoid.schemas.machine_need import MachineNeed
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import DeliverableKind, PlanProposal, ProposedStep, TaskStatus
from tests.support.cast import member
from tests.support.lead_turn import lead_plans

pytestmark = pytest.mark.integration

BROWSE = "Open https://news.ycombinator.com and tell me the top three stories"
MEMO = "Write a memo on our Q3 renewals pipeline for the leadership team"


@pytest.fixture(autouse=True)
def early_booting_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """A driver that boots early (`remote`, in `frames.EARLY_BOOT_DRIVERS`), with computer use on."""
    monkeypatch.setattr(frames, "EARLY_BOOT_DRIVERS", frozenset({"remote"}))
    monkeypatch.setattr(settings.worker, "frame_driver", "remote")
    monkeypatch.setattr(settings.machines, "enabled", True)
    monkeypatch.setattr(settings.machines, "boot_on_accept", True)
    monkeypatch.setattr(settings.computer_use, "enabled", True)


async def _task(goal: str, **dials: Any) -> uuid.UUID:
    async with session_scope() as session:
        actor, _ = await member(session, "Asker")
        task = await task_ops.create_task(session, actor, goal=goal, clarify="never", **dials)
        task.transition(TaskStatus.PLANNING)
        await session.commit()
        return task.id


async def _plan(task_id: uuid.UUID) -> tuple[TaskPlanned, bool]:
    """The mint as `plan_task` runs it: draft, commit, then the plan's boot."""
    async with row_scope(Task, task_id) as session:
        await planning.draft_plan(session, task_id)
        await session.commit()
        booted = await early_boot.for_plan(session, task_id)
        planned = await TaskEvent.latest_of_type(session, task_id, "task.planned")
    assert isinstance(planned, TaskPlanned)
    return planned, booted


async def _lead_planned(
    task_id: uuid.UUID, agent: str, *, hosts: tuple[str, ...] = (), publishes: bool = True
) -> TaskPlanned:
    """The Lead's turn: draft, then its run ends in a one-step plan on `agent`; the plan's `task.planned`."""
    step = ProposedStep(
        label="s1",
        agent=agent,
        title="The work",
        goal="Do the work",
        publishes=publishes,
        produces=ArtifactType.DOCUMENT if agent == "maker" else None,  # a maker makes several kinds
        hosts=list(hosts),
        browser=agent == "operator",
    )
    await lead_plans(task_id, PlanProposal(steps=[step], deliverable=DeliverableKind.FILES))
    async with row_scope(Task, task_id) as session:
        planned = await TaskEvent.latest_of_type(session, task_id, "task.planned")
    assert isinstance(planned, TaskPlanned) and planned.route_source == "lead"
    return planned


def _boots(recording: Any) -> list[str]:
    return [key for job, key in recording.enqueues if job == "machine_boot"]


async def _machine(task_id: uuid.UUID) -> Machine | None:
    async with row_scope(Task, task_id) as session:
        return await Machine.for_task(session, task_id)


# A reply boots nothing: the Lead's own step needs no computer, whatever it ends in (this test covers both).
async def test_a_talk_turn_boots_nothing_and_says_so(recording_dispatcher: Any) -> None:
    task_id = await _task("thanks!")
    planned, booted = await _plan(task_id)
    assert booted is False and _boots(recording_dispatcher) == []
    assert planned.route == "express" and planned.machine is MachineNeed.NONE
    assert await _machine(task_id) is None


@pytest.mark.parametrize("agent", ["writer", "researcher"])  # paper holds what they write
async def test_a_memo_plan_boots_no_early_machine(recording_dispatcher: Any, agent: str) -> None:
    task_id = await _task(MEMO)
    _, booted = await _plan(task_id)
    planned = await _lead_planned(task_id, agent)
    assert booted is False and _boots(recording_dispatcher) == []
    assert planned.machine is MachineNeed.FILES and agent in planned.machine_why


async def test_a_dialed_memo_boots_nothing(recording_dispatcher: Any) -> None:
    task_id = await _task(MEMO, deliverable=ArtifactType.DOCUMENT.value)
    _, booted = await _plan(task_id)
    assert booted is False and _boots(recording_dispatcher) == []


@pytest.mark.parametrize("agent", ["maker", "operator"])  # a retired name never reaches a new plan
async def test_computer_work_the_lead_plans_boots_once_the_plan_commits(
    recording_dispatcher: Any, agent: str
) -> None:
    task_id = await _task(MEMO)
    _, booted = await _plan(task_id)
    assert booted is False and _boots(recording_dispatcher) == [], "the Lead's step needs no computer"
    planned = await _lead_planned(task_id, agent)
    assert planned.machine is MachineNeed.COMPUTER and agent in planned.machine_why
    assert _boots(recording_dispatcher) == [f"machine-boot-{task_id}"]


async def test_a_browse_ask_boots_once_its_plan_commits_and_ends_with_one_warm_machine(
    recording_dispatcher: Any, fake_frames: Any
) -> None:
    """The Lead's plan commits with a browser step: the prewarm starts its Machine, and once the Machine
    exists nothing boots again."""
    task_id = await _task(BROWSE)
    first, booted = await _plan(task_id)
    assert first.route == "express" and booted is False
    planned = await _lead_planned(task_id, "operator", hosts=("news.ycombinator.com",))
    assert planned.machine is MachineNeed.COMPUTER
    assert _boots(recording_dispatcher) == [f"machine-boot-{task_id}"]
    assert await machines.boot(task_id) == "booted"
    async with row_scope(Task, task_id) as session:
        assert await early_boot.for_plan(session, task_id) is False, "a task with a Machine boots no other"
        assert await Machine.for_task(session, task_id) is not None  # one row per task, by a unique key


@pytest.mark.parametrize(("dial", "fmt"), [("spreadsheet", None), ("deck", None), ("document", "pdf")])
async def test_a_dial_for_computer_work_boots_once_its_plan_commits(
    recording_dispatcher: Any, dial: str, fmt: str | None
) -> None:
    task_id = await _task("Our 2027 numbers for the board", deliverable=dial, deliverable_format=fmt)
    assert _boots(recording_dispatcher) == [], "nothing boots before a plan commits"
    _, booted = await _plan(task_id)
    assert booted is True and _boots(recording_dispatcher) == [f"machine-boot-{task_id}"]


async def test_a_later_turn_with_no_machine_boots_under_its_own_id(recording_dispatcher: Any) -> None:
    """Turn 1 was talk, so the task has no Machine; turn 2's computer plan boots under a per-turn
    id, because a finished turn-1 boot id never runs again."""
    task_id = await _task(MEMO)
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        assert task is not None
        task.turn = 2
        await session.commit()
    await _lead_planned(task_id, "maker")
    assert _boots(recording_dispatcher) == [f"machine-boot-{task_id}-t2"]


@pytest.mark.parametrize("switch", ["boot_on_accept", "enabled"])
async def test_the_kill_switches_stop_every_boot(
    recording_dispatcher: Any, monkeypatch: pytest.MonkeyPatch, switch: str
) -> None:
    monkeypatch.setattr(settings.machines, switch, False)
    task_id = await _task(BROWSE)
    _, booted = await _plan(task_id)
    assert booted is False and _boots(recording_dispatcher) == []
