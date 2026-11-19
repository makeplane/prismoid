# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""No blind re-run; waiting for the device.

A device step that the durable engine recovers was running commands in the user's own folder, so it
never starts a fresh attempt on its own: it parks on a question-less PAUSE gate ("Waiting for <device
name>") until a person resumes the task, and the resumed attempt is told which command was cut off.
A helper that stays away past the grace parks the same way; a gap inside it parks nothing. Past
`away_limit_h` the task ends with what it had. A cloud Machine's recovery is unchanged. The rig is
`tests/support/rigs/device_recovery_rig.py`."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage
from sqlalchemy import update

from prismoid import storage
from prismoid.api.serializers.gates import gate_out
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.gate import Gate
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import execution, gates
from prismoid.schemas.device_away import DEVICE_GONE
from prismoid.schemas.events import (
    GateOpened,
    TaskStatusChanged,
    redact_for,
)
from prismoid.schemas.plan import GateKind, GateState, StepState, TaskStatus
from tests.support.dispatchers import RecordingDispatcher
from tests.support.fakes.fake_frames import DEVICE
from tests.support.rigs.device_recovery_rig import (
    APPEND,
    NOTE,
    Stream,
    WorkerDied,
    crashed_on_the_device,
    device_step,
    gates_of,
    journal_names,
    recovery_events,
    recovery_ran,
    recovery_row,
    recovery_run,
    recovery_spec,
    recovery_told,
    recovery_wake,
    resume_and_wake,
    step_wakes,
    working,
)
from tests.support.rigs.device_recovery_rig import private_database as private_database
from tests.support.rigs.device_recovery_rig import quick_grace as quick_grace
from tests.support.rigs.device_recovery_rig import real_agent as real_agent

pytestmark = pytest.mark.integration


async def test_recovered_device_step_parks_without_new_attempt(
    fake_frames: Any, silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    requests: list[list[ModelMessage]] = []
    task_id, step_id, _, _ = await crashed_on_the_device(fake_frames, real_agent, requests)
    before = await recovery_row(task_id, PlanStep, step_id)
    assert before.step_state is StepState.RUNNING and before.attempt == 1

    outcome = await recovery_run(task_id, step_id)  # the recovered workflow

    assert outcome.startswith(execution.ASKING)
    after = await recovery_row(task_id, PlanStep, step_id)
    assert (after.attempt, after.infra_attempts) == (before.attempt, before.infra_attempts), "no attempt"
    assert len(requests) == 2 and recovery_ran(fake_frames, APPEND) == 1, "no model call, no command again"
    (gate,) = await gates_of(task_id)
    assert str(gate.id) == outcome.removeprefix(execution.ASKING)
    assert gate.kind == GateKind.PAUSE.value and gate.state == GateState.OPEN.value
    assert gate.expires_at is None and gate.step_id == step_id
    assert gate.spec["title"] == "Waiting for Test Mac" and gate.spec["questions"] is None
    assert gate.spec["reason"] == (
        "device_away:interrupted\n"
        "Prismoid stopped in the middle of this step on Test Mac. It will not run again on its own: "
        "check the folder, then resume the task.\n"
        f"Cut off: `{APPEND}`"
    )
    assert (await recovery_row(task_id, Task, task_id)).state is TaskStatus.PAUSED
    assert after.resume_state is not None and after.resume_state["device_away"]["head"] == APPEND
    live = fake_frames.world[f"device-{task_id}"]
    assert live.cut is None and "ack_interrupted" in live.calls, "acknowledged once Postgres held it"

    # The recv's timeout, or another restart, re-parks on the same gate and asks nothing.
    assert await recovery_run(task_id, step_id) == outcome
    assert len(requests) == 2 and len(await gates_of(task_id)) == 1


async def test_a_command_still_running_parks_the_same_way(
    fake_frames: Any, silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    """Only the worker went away: the command still runs on the device. Prismoid neither waits for it
    nor kills it - the step parks, and the gate says it was still running."""
    task_id, step_id, _, _ = await device_step()
    real_agent(working([], dies=True))
    with pytest.raises(WorkerDied):
        await recovery_run(task_id, step_id)
    journal_names(fake_frames, still_running=True)

    assert (await recovery_run(task_id, step_id)).startswith(execution.ASKING)

    (gate,) = await gates_of(task_id)
    assert gate.spec["reason"].endswith(f"\nStill running when Prismoid came back: `{APPEND}`")
    assert "stop_services" not in fake_frames.world[f"device-{task_id}"].calls


async def test_resume_starts_attempt_with_interruption_note(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, real_agent: Callable[[Stream], None]
) -> None:
    requests: list[list[ModelMessage]] = []
    task_id, step_id, _, bob_id = await crashed_on_the_device(fake_frames, real_agent, requests)
    gate_id = (await recovery_run(task_id, step_id)).removeprefix(execution.ASKING)
    journal_names(fake_frames)  # a later crash's entry: the resume reads the note from Postgres

    await resume_and_wake(recording_dispatcher, bob_id, task_id, step_id)
    assert (await recovery_row(task_id, Task, task_id)).state is TaskStatus.RUNNING
    assert (await recovery_row(task_id, Gate, uuid.UUID(gate_id))).state == GateState.RESOLVED.value
    assert (await recovery_run(task_id, step_id)).strip() != ""

    assert NOTE in recovery_told(requests[2]), "the resumed attempt is told what was cut off"
    assert NOTE not in recovery_told(requests[0])
    assert recovery_ran(fake_frames, APPEND) == 1, "the cut-off command never ran again"
    done = await recovery_row(task_id, PlanStep, step_id)
    assert done.step_state is StepState.DONE and done.attempt == 2
    assert done.model_attempts() == 1, "the crash and its park never spend the ladder"


async def test_a_command_that_finished_unseen_is_told_on_resume(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, real_agent: Callable[[Stream], None]
) -> None:
    """It ended after the worker left, so its result reached nobody. The gate and the resumed attempt
    say it finished, with its exit code, never "cut off"; the note outlives the park's state."""
    requests: list[list[ModelMessage]] = []
    task_id, step_id, _, bob_id = await crashed_on_the_device(fake_frames, real_agent, requests)
    journal_names(fake_frames, exit_code=3)

    gate_id = (await recovery_run(task_id, step_id)).removeprefix(execution.ASKING)

    gate = await recovery_row(task_id, Gate, uuid.UUID(gate_id))
    assert gate.spec["reason"].endswith(f"\nFinished while Prismoid was away (exit 3): `{APPEND}`")
    await resume_and_wake(recording_dispatcher, bob_id, task_id, step_id)
    assert (await recovery_run(task_id, step_id)).strip() != ""
    told = recovery_told(requests[2])
    assert (
        f"Your last command finished while Prismoid was away, and you never saw its result (exit 3): `{APPEND}`"
        in told
    )
    assert "cut off" not in told
    assert recovery_ran(fake_frames, APPEND) == 1, "told it finished, it does not run it again"


async def test_a_park_that_could_not_ask_the_helper_reads_it_on_resume(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, real_agent: Callable[[Stream], None]
) -> None:
    """The worker and the helper died together, and the helper is not back when the workflow
    recovers: the step parks all the same, and the resumed attempt asks the journal itself - then
    acknowledges it once its prompt is on the record."""
    requests: list[list[ModelMessage]] = []
    task_id, step_id, _, bob_id = await crashed_on_the_device(fake_frames, real_agent, requests)
    fake_frames.unreachable = True
    assert (await recovery_run(task_id, step_id)).startswith(execution.ASKING)
    (gate,) = await gates_of(task_id)
    assert "Cut off" not in gate.spec["reason"], "nothing read, nothing claimed"
    state = (await recovery_row(task_id, PlanStep, step_id)).resume_state
    assert state is not None and state["device_away"]["look"] is True

    fake_frames.unreachable = False
    await resume_and_wake(recording_dispatcher, bob_id, task_id, step_id)
    assert (await recovery_run(task_id, step_id)).strip() != ""

    assert NOTE in recovery_told(requests[2])
    live = fake_frames.world[f"device-{task_id}"]
    assert live.cut is None and "ack_interrupted" in live.calls


async def test_the_cut_off_command_is_the_creators_to_read(
    fake_frames: Any, silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    """A command may carry a secret: an editor of the Project reads why the step waits, never it."""
    task_id, step_id, ada_id, bob_id = await crashed_on_the_device(fake_frames, real_agent, [])
    await recovery_run(task_id, step_id)

    (opened,) = [e for e in await recovery_events(task_id, GateOpened) if e.reason.startswith("device_away:")]
    assert APPEND in redact_for(opened, viewer_id=ada_id, creator_id=ada_id).reason
    seen = redact_for(opened, viewer_id=bob_id, creator_id=ada_id).reason
    assert seen.startswith("device_away:interrupted\nPrismoid stopped") and APPEND not in seen
    (gate,) = await gates_of(task_id)
    assert APPEND in gate_out(gate, viewer_id=ada_id, creator_actor_id=ada_id).reason
    assert APPEND not in gate_out(gate, viewer_id=bob_id, creator_actor_id=ada_id).reason


async def test_a_device_step_recovered_in_its_review_parks_then_resumes_at_the_review(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, real_agent: Callable[[Stream], None]
) -> None:
    """The VERIFYING re-entry, on the device: it parks like any recovered step, and the
    person's resume owes only the review - no attempt, no model call."""
    task_id, step_id, _, bob_id = await device_step()
    body = b"# fix\n"
    digest = storage.put(body)
    async with row_scope(Task, task_id) as s:
        step = await s.get(PlanStep, step_id)
        assert step is not None
        step.state, step.attempt = StepState.VERIFYING.value, 1
        s.add(
            Artifact(
                id=uuid.uuid4(),
                lineage_id=uuid.uuid4(),
                task_id=task_id,
                step_id=step_id,
                type="document",
                title="Fix",
                sha256=digest,
                bytes=len(body),
                storage_key=storage.key_for(digest),
                provenance={"path": "/artifacts/fix.md"},
            )
        )
        await s.commit()
    requests: list[list[ModelMessage]] = []
    real_agent(working(requests, dies=False))

    gate_id = (await recovery_run(task_id, step_id)).removeprefix(execution.ASKING)
    assert (await recovery_row(task_id, PlanStep, step_id)).step_state is StepState.VERIFYING
    assert await recovery_run(task_id, step_id) == f"{execution.ASKING}{gate_id}", "re-parks from its review"

    await resume_and_wake(recording_dispatcher, bob_id, task_id, step_id)
    await recovery_run(task_id, step_id)

    done = await recovery_row(task_id, PlanStep, step_id)
    assert done.step_state is StepState.DONE and done.attempt == 1 and requests == []


async def test_cloud_recovery_unchanged(
    fake_frames: Any, silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    """A Docker Machine's recovered step goes on at once, in the attempt it died in: its call log
    replays what the attempt bought, the command included, and only the request the worker died
    inside is sent again (ADR-089)."""
    requests: list[list[ModelMessage]] = []
    task_id, step_id, _, _ = await device_step(driver=None)
    real_agent(working(requests, dies=True))
    with pytest.raises(WorkerDied):
        await recovery_run(task_id, step_id)

    assert (await recovery_run(task_id, step_id)).strip() != ""

    assert await gates_of(task_id) == []
    assert len(requests) == 5 and "cut off" not in recovery_told(requests[2])
    assert recovery_ran(fake_frames, APPEND) == 1, "the logged command is replayed, never run again"
    done = await recovery_row(task_id, PlanStep, step_id)
    assert done.step_state is StepState.DONE and done.attempt == 1 and done.model_attempts() == 1


async def test_helper_unreachable_parks_away(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    real_agent: Callable[[Stream], None],
    quick_grace: None,
) -> None:
    """The helper does not answer for longer than the grace: the step parks instead of burning infra
    rungs, and the person's resume buys the attempt the away one never ran."""
    requests: list[list[ModelMessage]] = []
    task_id, step_id, _, bob_id = await device_step()
    real_agent(working(requests, dies=False))
    fake_frames.unreachable = True

    outcome = await recovery_run(task_id, step_id)

    assert outcome.startswith(execution.ASKING) and requests == []
    (gate,) = await gates_of(task_id)
    assert gate.kind == GateKind.PAUSE.value and gate.expires_at is None
    assert gate.spec["title"] == "Waiting for your computer", "no Machine has named its device yet"
    assert gate.spec["reason"] == (
        "device_away:away\nPrismoid cannot reach your computer. The step waits; resume the task once it is back."
    )
    assert (await recovery_row(task_id, Task, task_id)).state is TaskStatus.PAUSED

    fake_frames.unreachable = False
    await resume_and_wake(recording_dispatcher, bob_id, task_id, step_id)
    assert (await recovery_run(task_id, step_id)).strip() != ""
    done = await recovery_row(task_id, PlanStep, step_id)
    assert done.step_state is StepState.DONE and done.model_attempts() == 1
    assert NOTE not in recovery_told(requests[0]), "an away park cut nothing off"


async def test_short_helper_gap_does_not_park(
    fake_frames: Any, silent_dispatcher: None, real_agent: Callable[[Stream], None], quick_grace: None
) -> None:
    """The Mac slept a while (the helper unreachable well inside the grace), then
    woke. The step carries on - no gate, no pause, no extra rung."""
    requests: list[list[ModelMessage]] = []
    task_id, step_id, _, _ = await device_step()
    real_agent(working(requests, dies=False))
    fake_frames.unreachable = True

    async def wake_up() -> None:
        await asyncio.sleep(0.2)
        fake_frames.unreachable = False

    back = asyncio.create_task(wake_up())
    outcome = await recovery_run(task_id, step_id)
    await back

    assert not outcome.startswith(execution.ASKING) and await gates_of(task_id) == []
    assert (await recovery_row(task_id, Task, task_id)).state is TaskStatus.RUNNING
    done = await recovery_row(task_id, PlanStep, step_id)
    assert done.step_state is StepState.DONE and done.attempt == 1


async def test_away_past_limit_ends_task_with_reason(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    real_agent: Callable[[Stream], None],
    quick_grace: None,
) -> None:
    """Past `away_limit_h` the task ends with what it had: failed, reading the reason, the artifact
    an earlier step published still the task's, and the parked step woken to take its cancel exit."""
    task_id, step_id, _, _ = await device_step()
    body = b"# notes\n"
    digest = storage.put(body)
    async with row_scope(Task, task_id) as s:
        earlier = PlanStep(
            id=uuid.uuid4(),
            task_id=task_id,
            ordinal=1,
            kind="step",
            state="done",
            title="notes",
            spec=recovery_spec("n"),
        )
        s.add(earlier)
        await s.flush()
        kept = Artifact(
            id=uuid.uuid4(),
            lineage_id=uuid.uuid4(),
            task_id=task_id,
            step_id=earlier.id,
            type="document",
            title="Notes",
            sha256=digest,
            bytes=len(body),
            storage_key=storage.key_for(digest),
            provenance={"path": "/artifacts/notes.md"},
        )
        s.add(kept)
        await s.commit()
    real_agent(working([], dies=False))
    fake_frames.unreachable = True
    gate_id = uuid.UUID((await recovery_run(task_id, step_id)).removeprefix(execution.ASKING))

    limit = dt.timedelta(hours=settings.local.away_limit_h)
    async with session_scope(platform=True) as s:
        assert gate_id not in [g.id for g in await Gate.device_away_before(s, utcnow() - limit)], "not yet"
    async with row_scope(Task, task_id) as s:
        await s.execute(
            update(Gate)
            .where(Gate.id == gate_id)
            .values(created_at=utcnow() - limit - dt.timedelta(minutes=1))
        )
        await s.commit()
    async with session_scope(platform=True) as s:
        (gone,) = [g for g in await Gate.device_away_before(s, utcnow() - limit) if g.id == gate_id]
        assert await gates.end_device_gone(s, gone)

    task = await recovery_row(task_id, Task, task_id)
    assert task.state is TaskStatus.FAILED
    assert [
        e.reason for e in await recovery_events(task_id, TaskStatusChanged) if e.status is TaskStatus.FAILED
    ] == [DEVICE_GONE]
    assert (await recovery_row(task_id, Gate, gate_id)).state == GateState.EXPIRED.value
    (wake,) = step_wakes(recording_dispatcher, step_id)
    assert wake["gate_id"] == str(gate_id) and wake["action"] == "cancel"
    await recovery_wake(task_id, step_id, wake)  # the parked workflow's cancel exit
    async with row_scope(Task, task_id) as s:
        await execution.close_cancelled_park(s, task_id, step_id)
        assert [a.id for a in await Artifact.for_step(s, earlier.id)] == [kept.id], "what it had stays"
    async with session_scope(platform=True) as s:
        again = await s.get(Gate, gate_id)
        assert again is not None and await gates.end_device_gone(s, again) is False, "it ends once"


async def test_a_worker_whose_own_driver_is_the_device_parks_too(
    fake_frames: Any,
    silent_dispatcher: None,
    real_agent: Callable[[Stream], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I1: a task that names no driver runs on the worker's - here the device - and its recovered
    step parks like one that names it. (Made before the worker's driver is the device: a device
    task is never filed in a Project with members, E1's private-only rule, and this one's is.)"""
    requests: list[list[ModelMessage]] = []
    task_id, step_id, _, _ = await device_step(driver=None)
    monkeypatch.setattr(settings.worker, "frame_driver", DEVICE)
    real_agent(working(requests, dies=True))
    with pytest.raises(WorkerDied):
        await recovery_run(task_id, step_id)

    assert (await recovery_run(task_id, step_id)).startswith(execution.ASKING)
    assert len(requests) == 2 and recovery_ran(fake_frames, APPEND) == 1
