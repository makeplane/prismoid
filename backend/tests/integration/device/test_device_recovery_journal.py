# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The helper's journal on recovery: a command left running stops the
next attempt, a journal that cannot be read parks, and a helper that cannot be asked is left to the
acquire. The rig is `tests/support/rigs/device_recovery_rig.py`."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage

from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.operations import execution
from prismoid.operations.execution import device as device_ops
from prismoid.schemas.machines import FrameDriverName
from prismoid.schemas.plan import StepState, TaskStatus
from tests.support.dispatchers import RecordingDispatcher
from tests.support.fakes.fake_frames import DEVICE
from tests.support.rigs.device_recovery_rig import (
    APPEND,
    Stream,
    beside_a_refusal,
    device_step,
    gates_of,
    journal_names,
    recovery_row,
    recovery_run,
    recovery_told,
    refuse_reads,
    resume_and_wake,
)
from tests.support.rigs.device_recovery_rig import private_database as private_database
from tests.support.rigs.device_recovery_rig import quick_grace as quick_grace
from tests.support.rigs.device_recovery_rig import real_agent as real_agent

pytestmark = pytest.mark.integration


async def _refused_beside(
    fake_frames: Any,
    monkeypatch: pytest.MonkeyPatch,
    real_agent: Callable[[Stream], None],
    requests: list[list[ModelMessage]],
    driver: FrameDriverName | None,
    *,
    still_running: bool,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]:
    """A device (or cloud) step whose first attempt ends on a refused sibling; on the device the
    helper's journal names the command as the helper goes on running it, or as ended."""

    async def refused() -> None:
        journal_names(fake_frames, still_running=still_running)

    task_id, step_id, ada_id, _ = await device_step(driver)
    refuse_reads(monkeypatch, once=refused)
    real_agent(beside_a_refusal(requests))
    return task_id, step_id, ada_id, await recovery_run(task_id, step_id)


async def test_a_command_left_running_stops_the_next_attempt(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    monkeypatch: pytest.MonkeyPatch,
    real_agent: Callable[[Stream], None],
) -> None:
    """A sibling's refusal ends the attempt and only the worker's side of its command:
    the next rung parks on what the journal names, and starts nothing beside it. The person's resume
    goes on: told the command, never parked on it again, even while it still runs."""
    requests: list[list[ModelMessage]] = []
    task_id, step_id, ada_id, outcome = await _refused_beside(
        fake_frames, monkeypatch, real_agent, requests, DEVICE, still_running=True
    )

    assert outcome.startswith(execution.ASKING), outcome
    assert len(requests) == 1, "no second attempt started"
    (gate,) = await gates_of(task_id)
    assert gate.spec["reason"].startswith("device_away:interrupted\n")
    assert f"Still running when Prismoid came back: `{APPEND}`" in gate.spec["reason"]
    assert (await recovery_row(task_id, Task, task_id)).state is TaskStatus.PAUSED
    assert (await recovery_row(task_id, PlanStep, step_id)).attempt == 1

    await resume_and_wake(recording_dispatcher, ada_id, task_id, step_id)
    await recovery_run(task_id, step_id)

    assert any("still running when Prismoid recovered" in recovery_told(r) for r in requests[1:])
    done = await recovery_row(task_id, PlanStep, step_id)
    assert done.step_state is StepState.DONE
    assert len(await gates_of(task_id)) == 1, "the resume is not parked on what it was told"


async def test_a_park_between_rungs_keeps_what_the_last_attempt_learned(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    monkeypatch: pytest.MonkeyPatch,
    real_agent: Callable[[Stream], None],
) -> None:
    """The first attempt failed, and the next rung parked beside its command. The attempt a person
    resumes is told why the last one failed, as any retry is: a park is not a fresh start."""
    requests: list[list[ModelMessage]] = []
    task_id, step_id, ada_id, outcome = await _refused_beside(
        fake_frames, monkeypatch, real_agent, requests, DEVICE, still_running=True
    )
    assert outcome.startswith(execution.ASKING), outcome

    await resume_and_wake(recording_dispatcher, ada_id, task_id, step_id)
    await recovery_run(task_id, step_id)

    assert any("Your previous attempt" in recovery_told(r) for r in requests[1:])


async def test_a_command_that_ended_stops_nothing(
    fake_frames: Any,
    silent_dispatcher: None,
    monkeypatch: pytest.MonkeyPatch,
    real_agent: Callable[[Stream], None],
) -> None:
    """The journal's entry is open but the command is not running in this helper life: the ladder
    goes on, as for any failed attempt (nothing runs beside the next one)."""
    requests: list[list[ModelMessage]] = []
    task_id, _, _, _ = await _refused_beside(
        fake_frames, monkeypatch, real_agent, requests, DEVICE, still_running=False
    )
    assert len(requests) > 1, "the next rung started"
    assert not await gates_of(task_id)


async def test_a_cloud_rung_never_asks_a_journal(
    fake_frames: Any,
    silent_dispatcher: None,
    monkeypatch: pytest.MonkeyPatch,
    real_agent: Callable[[Stream], None],
) -> None:
    """The same refused sibling on a cloud Machine: the ladder is as it was, and nothing asks."""

    async def never(*_: object) -> None:
        raise AssertionError("a cloud task asked a device journal")

    monkeypatch.setattr(device_ops.machines, "interrupted", never)
    requests: list[list[ModelMessage]] = []
    task_id, _, _, _ = await _refused_beside(
        fake_frames, monkeypatch, real_agent, requests, None, still_running=True
    )
    assert len(requests) > 1, "the next rung started"
    assert not await gates_of(task_id)


async def test_a_journal_that_cannot_be_read_parks_and_leaves_the_attempt_to_look(
    fake_frames: Any,
    silent_dispatcher: None,
    monkeypatch: pytest.MonkeyPatch,
    real_agent: Callable[[Stream], None],
) -> None:
    """A helper that answers and a journal that does not read: nothing can say the folder is quiet,
    so no attempt is bought on a guess."""

    async def unread(*_: object) -> None:
        raise ValueError("not a journal entry")

    async def bad_journal() -> None:
        monkeypatch.setattr(device_ops.machines, "interrupted", unread)

    requests: list[list[ModelMessage]] = []
    task_id, step_id, _, _ = await device_step()
    refuse_reads(monkeypatch, once=bad_journal)
    real_agent(beside_a_refusal(requests))

    outcome = await recovery_run(task_id, step_id)

    assert outcome.startswith(execution.ASKING), outcome
    assert len(requests) == 1
    (gate,) = await gates_of(task_id)
    assert gate.spec["reason"].startswith("device_away:interrupted\n")


async def test_a_journal_that_keeps_failing_parks_once_and_the_resume_goes_through(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    monkeypatch: pytest.MonkeyPatch,
    real_agent: Callable[[Stream], None],
) -> None:
    """The read stays broken (a helper of another version, say). A person has looked and resumed:
    the resume is theirs to give, and it is not parked again on what still cannot be read."""

    async def unread(*_: object) -> None:
        raise ValueError("not a journal entry")

    async def bad_journal() -> None:
        monkeypatch.setattr(device_ops.machines, "interrupted", unread)

    requests: list[list[ModelMessage]] = []
    task_id, step_id, ada_id, _ = await device_step()
    refuse_reads(monkeypatch, once=bad_journal)
    real_agent(beside_a_refusal(requests))

    assert (await recovery_run(task_id, step_id)).startswith(execution.ASKING)
    assert len(requests) == 1

    await resume_and_wake(recording_dispatcher, ada_id, task_id, step_id)
    await recovery_run(task_id, step_id)

    assert (await recovery_row(task_id, PlanStep, step_id)).step_state is StepState.DONE
    assert len(await gates_of(task_id)) == 1, "one park, and the resume went through"


async def test_a_resume_owed_a_named_command_is_honoured_when_the_journal_fails(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unread(*_: object) -> None:
        raise ValueError("not a journal entry")

    monkeypatch.setattr(device_ops.machines, "interrupted", unread)
    task_id, step_id, _, _ = await device_step()
    async with row_scope(Task, task_id) as s:
        task, step = await s.get_one(Task, task_id), await s.get_one(PlanStep, step_id)
        assert await device_ops.left_running(s, task, step) == device_ops.Cut(look=True), "nobody resumed"
        step.resume_state = device_ops.owed_state(step, device_ops.Cut(head="make", exec_id="e1"))
        assert await device_ops.left_running(s, task, step) is None


async def test_a_helper_that_cannot_be_asked_is_left_to_the_acquire(
    fake_frames: Any,
    silent_dispatcher: None,
    quick_grace: None,
    monkeypatch: pytest.MonkeyPatch,
    real_agent: Callable[[Stream], None],
) -> None:
    """The helper gone between the rungs: the journal cannot be read, and the acquire that follows
    has the grace. Past it the step waits for its device, as any acquire does."""

    async def gone() -> None:
        fake_frames.unreachable = True

    task_id, step_id, _, _ = await device_step()
    refuse_reads(monkeypatch, once=gone)
    requests: list[list[ModelMessage]] = []
    real_agent(beside_a_refusal(requests))

    outcome = await recovery_run(task_id, step_id)

    fake_frames.unreachable = False
    assert outcome.startswith(execution.ASKING), outcome
    (gate,) = await gates_of(task_id)
    assert gate.spec["reason"].startswith("device_away:away\n")
    assert len(requests) == 1
