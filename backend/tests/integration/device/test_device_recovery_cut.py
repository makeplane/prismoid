# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A device step cut off mid-command: the park keeps its conversation, a
wall-clock stop and a dying helper park the step, the wait knocks the creator's inbox, and the sweep ends
what nobody resumed. The rig is `tests/support/rigs/device_recovery_rig.py`."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCalls
from sqlalchemy import select, update

import prismoid.ai.agents.base as agents_base
from prismoid.ai.frames.driver import FrameUnreachable
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.gate import Gate
from prismoid.db.models.inbox import InboxItem, InboxKind
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.errors import ModelIncapable
from prismoid.operations import execution, gates
from prismoid.operations.execution import acquire as ex_acquire
from prismoid.schemas.device_away import DEVICE_GONE
from prismoid.schemas.events import (
    StepPrompt,
    TaskStatusChanged,
)
from prismoid.schemas.machines import MachineState, holder_step
from prismoid.schemas.plan import StepState, TaskStatus
from tests.support.dispatchers import RecordingDispatcher
from tests.support.fakes.fake_frames import NoneSleepFakeFrame
from tests.support.rigs.device_recovery_rig import (
    APPEND,
    NOTE,
    PUBLISH,
    WRITE,
    Stream,
    WorkerDied,
    call,
    crashed_on_the_device,
    device_step,
    gates_of,
    journal_names,
    recovery_events,
    recovery_ran,
    recovery_row,
    recovery_run,
    recovery_told,
    resume_and_wake,
    step_wakes,
    working,
)
from tests.support.rigs.device_recovery_rig import private_database as private_database
from tests.support.rigs.device_recovery_rig import quick_grace as quick_grace
from tests.support.rigs.device_recovery_rig import real_agent as real_agent

pytestmark = pytest.mark.integration


async def test_a_cut_owed_survives_a_rung_that_dies_before_its_prompt(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    real_agent: Callable[[Stream], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I2: the first rung after the resume cannot get its Machine and is paced - a hand-back to the
    workflow, a new `execute_step` after it. The rung after the pace is still told what was cut off."""
    requests: list[list[ModelMessage]] = []
    task_id, step_id, _, bob_id = await crashed_on_the_device(fake_frames, real_agent, requests)
    await recovery_run(task_id, step_id)
    await resume_and_wake(recording_dispatcher, bob_id, task_id, step_id)
    real_acquire, calls = ex_acquire.acquire_for, []

    async def busy_once(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise ModelIncapable("the Machine is busy", infra=True)
        return await real_acquire(*args, **kwargs)

    monkeypatch.setattr(ex_acquire, "acquire_for", busy_once)
    monkeypatch.setattr(settings.machines, "wait_s", 30)

    assert (await recovery_run(task_id, step_id)).startswith(execution.PACING)
    assert (await recovery_run(task_id, step_id)).strip() != ""  # the paced rung, after the workflow's sleep

    assert any(NOTE in recovery_told(r) for r in requests[2:]), (
        "the note waited for a rung that reached its prompt"
    )
    assert recovery_ran(fake_frames, APPEND) == 1
    done = await recovery_row(task_id, PlanStep, step_id)
    assert done.step_state is StepState.DONE and done.resume_state is None


@pytest.fixture
def guarded_coder(monkeypatch: pytest.MonkeyPatch) -> None:
    """The maker, guarded: a continued retry without a browser (the `test_guarded_retry` way)."""
    maker = agents_base.get("maker")
    monkeypatch.setitem(agents_base.REGISTRY, maker.name, replace(maker, guarded=True))


async def test_an_away_park_keeps_the_dead_run_and_the_continued_run_hears_the_cut(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    real_agent: Callable[[Stream], None],
    guarded_coder: None,
    quick_grace: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M11 and I2's continued branch: a guarded attempt stops at its request ceiling, the next one
    finds the device away and parks. The person's resume continues that run on its call log, and is
    told the cut a park held (written here as an interrupted park holds it)."""
    task_id, step_id, _, bob_id = await device_step()
    monkeypatch.setattr(settings.worker, "request_ceiling", 2)
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        if len(requests) <= 2:
            yield call(*WRITE, f"w{len(requests)}")  # the second request is the ceiling's last
        elif len(requests) == 3:
            yield call(*PUBLISH, "p1")
        else:
            yield "done"

    real_acquire, acquired = ex_acquire.acquire_for, []

    async def away_on_attempt_2(*args: Any, **kwargs: Any) -> Any:
        acquired.append(1)
        if len(acquired) == 2:
            fake_frames.unreachable = True  # the device goes before the next attempt's Machine
        return await real_acquire(*args, **kwargs)

    monkeypatch.setattr(ex_acquire, "acquire_for", away_on_attempt_2)
    real_agent(stream)
    assert (await recovery_run(task_id, step_id)).startswith(execution.ASKING)
    state = (await recovery_row(task_id, PlanStep, step_id)).resume_state
    assert state is not None and state["owed_run"]["pickup"] == "continue", "the dead run rides the record"
    async with row_scope(Task, task_id) as s:
        step = await s.get(PlanStep, step_id)
        assert step is not None and step.resume_state is not None
        away = {**step.resume_state["device_away"], "head": APPEND, "exec_id": "e1"}
        step.resume_state = {**step.resume_state, "device_away": away}
        await s.commit()

    fake_frames.unreachable = False
    await resume_and_wake(recording_dispatcher, bob_id, task_id, step_id)
    assert (await recovery_run(task_id, step_id)).strip() != ""

    continued = requests[2]
    assert {"w1", "w2"} <= {
        p.tool_call_id
        for m in continued
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart)
    }, "attempt 1's run, replayed from its call log, never bought again"
    assert len(requests) == 4
    assert "Your previous attempt stopped:" in recovery_told(continued) and NOTE in recovery_told(continued)
    prompts = [e.prompt for e in await recovery_events(task_id, StepPrompt)]
    assert NOTE in prompts[-1], "the receipt shows the note"


async def test_the_helper_dying_mid_command_parks_the_step(
    fake_frames: Any,
    silent_dispatcher: None,
    real_agent: Callable[[Stream], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I3: the helper dies under the command while the worker lives. The step parks - the attempt a
    person resumes reads the journal (`look`) - never a failed task, never another attempt."""
    task_id, step_id, _, _ = await device_step()
    requests: list[list[ModelMessage]] = []
    real_agent(working(requests, dies=False))
    real_exec = NoneSleepFakeFrame.exec

    async def helper_dies(self: Any, command: str, timeout: float = 120.0) -> Any:  # noqa: ASYNC109
        if command == APPEND:
            raise FrameUnreachable("the device helper did not answer")
        return await real_exec(self, command, timeout)

    monkeypatch.setattr(NoneSleepFakeFrame, "exec", helper_dies)

    assert (await recovery_run(task_id, step_id)).startswith(execution.ASKING)

    (gate,) = await gates_of(task_id)
    assert gate.spec["reason"].startswith("device_away:interrupted\n")
    assert (await recovery_row(task_id, Task, task_id)).state is TaskStatus.PAUSED
    state = (await recovery_row(task_id, PlanStep, step_id)).resume_state
    assert state is not None and state["device_away"]["look"] is True
    assert len(requests) == 1 and (await recovery_row(task_id, PlanStep, step_id)).attempt == 1


async def test_a_wall_clock_stop_on_the_device_parks_the_step(
    fake_frames: Any,
    silent_dispatcher: None,
    real_agent: Callable[[Stream], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M5: the wall clock stops a device attempt whose command may still run there. It parks
    (`look`), never an infra rung that would start the next attempt beside it."""
    monkeypatch.setattr(settings.worker, "agent_wall_clock_s", 1)
    task_id, step_id, _, _ = await device_step()
    requests: list[list[ModelMessage]] = []

    async def wedged(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        if len(requests) > 1:  # bounded: an infra rung nets out of every cap, so it would go on for ever
            raise AssertionError("a second attempt started beside the first one's command")
        await asyncio.sleep(5)
        yield "never"

    real_agent(wedged)

    assert (await recovery_run(task_id, step_id)).startswith(execution.ASKING)
    (gate,) = await gates_of(task_id)
    assert gate.spec["reason"].startswith("device_away:interrupted\n")
    step = await recovery_row(task_id, PlanStep, step_id)
    assert step.attempt == 1 and len(requests) == 1 and step.resume_state is not None
    assert step.resume_state["device_away"]["look"] is True


async def test_a_command_still_running_is_told_so_on_resume(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, real_agent: Callable[[Stream], None]
) -> None:
    """M7: only the worker went away, so the command ran on. The resumed attempt is told it was still
    running - to check whether it finished - never that it was cut off."""
    requests: list[list[ModelMessage]] = []
    task_id, step_id, _, bob_id = await device_step()
    real_agent(working(requests, dies=True))
    with pytest.raises(WorkerDied):
        await recovery_run(task_id, step_id)
    journal_names(fake_frames, still_running=True)
    await recovery_run(task_id, step_id)
    state = (await recovery_row(task_id, PlanStep, step_id)).resume_state
    assert state is not None and state["device_away"]["still_running"] is True
    assert state["device_away"]["exec_id"] == "e1"

    await resume_and_wake(recording_dispatcher, bob_id, task_id, step_id)
    await recovery_run(task_id, step_id)

    told = recovery_told(requests[2])
    assert (
        f"Your last command was still running when Prismoid recovered: `{APPEND}`. Check whether it finished "
        "(git status, the files it touched) before you act."
    ) in told
    assert "was cut off" not in told


async def test_the_waiting_step_keeps_its_machine_parked(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, real_agent: Callable[[Stream], None]
) -> None:
    """M4: while the step waits the Machine is the step's park - `parked`, held - so the idle sleep's
    ten minutes never stop a command still running there. The person's resume binds it again."""
    task_id, step_id, _, bob_id = await crashed_on_the_device(fake_frames, real_agent, [])
    await recovery_run(task_id, step_id)

    async with row_scope(Task, task_id) as s:
        row = await Machine.for_task(s, task_id)
        assert row is not None
        held = holder_step(row.held_by)
        assert row.state == MachineState.PARKED and held is not None and held[0] == str(step_id)

    await resume_and_wake(recording_dispatcher, bob_id, task_id, step_id)
    await recovery_run(task_id, step_id)
    async with row_scope(Task, task_id) as s:
        row = await Machine.for_task(s, task_id)
        assert row is not None and row.state == MachineState.IDLE and row.held_by is None


async def test_a_device_wait_is_knocked_into_the_creators_inbox(
    fake_frames: Any, silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    """I4: nobody asked for this pause, so the creator hears of it - the command line left out."""
    task_id, step_id, ada_id, _ = await crashed_on_the_device(fake_frames, real_agent, [])
    gate_id = uuid.UUID((await recovery_run(task_id, step_id)).removeprefix(execution.ASKING))

    async with row_scope(Task, task_id) as s:
        items = list(await s.scalars(select(InboxItem).where(InboxItem.task_id == task_id)))
        knocked = [i for i in items if i.kind == InboxKind.GATE_OPENED.value]
        assert [i.actor_id for i in knocked] == [ada_id]
        (item,) = knocked
        assert item.gate_id == gate_id and item.title == "Waiting for Test Mac"
        assert item.body.startswith("Prismoid stopped in the middle of this step") and APPEND not in item.body


async def test_device_waits_never_use_up_the_pause_cap(
    fake_frames: Any, silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    """M1: `max_pause_parks` is the cap people pause under; a step waiting for its device is not in it."""
    async with session_scope(platform=True) as s:
        before = await Gate.open_pause_count(s)
    task_id, step_id, _, _ = await crashed_on_the_device(fake_frames, real_agent, [])
    await recovery_run(task_id, step_id)

    assert len(await gates_of(task_id)) == 1
    async with session_scope(platform=True) as s:
        assert await Gate.open_pause_count(s) == before


async def _aged(task_id: uuid.UUID, gate_id: uuid.UUID) -> None:
    limit = dt.timedelta(hours=settings.local.away_limit_h)
    async with row_scope(Task, task_id) as s:
        await s.execute(
            update(Gate)
            .where(Gate.id == gate_id)
            .values(created_at=utcnow() - limit - dt.timedelta(minutes=1))
        )
        await s.commit()


async def test_the_sweep_ends_an_interrupted_step_nobody_resumed(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, real_agent: Callable[[Stream], None]
) -> None:
    """M9 and M10, through the sweep itself: a step the crash cut off that nobody resumed in time
    ends its task saying so - the device may well be back."""
    task_id, step_id, _, _ = await crashed_on_the_device(fake_frames, real_agent, [])
    gate_id = uuid.UUID((await recovery_run(task_id, step_id)).removeprefix(execution.ASKING))
    await _aged(task_id, gate_id)

    async with session_scope(platform=True) as s:
        assert gate_id in await gates.sweep(s)

    assert (await recovery_row(task_id, Task, task_id)).state is TaskStatus.FAILED
    reasons = [
        e.reason for e in await recovery_events(task_id, TaskStatusChanged) if e.status is TaskStatus.FAILED
    ]
    assert reasons == [f"stopped: not resumed within {settings.local.away_limit_h} hours"]
    (wake,) = step_wakes(recording_dispatcher, step_id)
    assert wake["action"] == "cancel"


async def test_the_sweep_ends_a_device_that_never_came_back(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    real_agent: Callable[[Stream], None],
    quick_grace: None,
) -> None:
    """M10: the away arm through the sweep, its reason exact."""
    task_id, step_id, _, _ = await device_step()
    real_agent(working([], dies=False))
    fake_frames.unreachable = True
    gate_id = uuid.UUID((await recovery_run(task_id, step_id)).removeprefix(execution.ASKING))
    await _aged(task_id, gate_id)

    async with session_scope(platform=True) as s:
        assert gate_id in await gates.sweep(s)

    reasons = [
        e.reason for e in await recovery_events(task_id, TaskStatusChanged) if e.status is TaskStatus.FAILED
    ]
    assert reasons == [DEVICE_GONE]
