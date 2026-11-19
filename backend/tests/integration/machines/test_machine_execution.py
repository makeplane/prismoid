# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Serial steps share the task's Machine; a pre-1b park resumes on its own frame.

The real `execute_step` against real rows, with the model stubbed (the `test_express_lane.py`
pattern) and every frame a `FakeFrame`. The rig is `tests/support/rigs/machine_execution_rig.py`; the boot a turn's
pick-up starts is `tests/integration/machines/test_machine_execution_boot.py`."""

import asyncio
import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic_ai import DeferredToolRequests
from pydantic_ai.messages import ModelResponse, ToolCallPart
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.tools.ask import ASK_USER
from prismoid.ai.tools.browser import PAGE_RESET
from prismoid.conf import settings
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanEdge, PlanStep
from prismoid.db.models.task import Task
from prismoid.db.models.usage import UsageEvent
from prismoid.db.session import row_scope
from prismoid.errors import PolicyDenied, StepFailed
from prismoid.operations import execution, machines
from prismoid.operations.execution import acquire as ex_acquire
from prismoid.operations.execution import giveback as ex_giveback
from prismoid.operations.execution import parks as ex_parks
from prismoid.schemas.machines import MachineState, SleepReason
from prismoid.schemas.plan import StepState, TaskStatus
from tests.support.rigs.machine_execution_rig import (
    Run,
    step_machine,
    step_publish,
    step_run,
    task_with_steps,
)
from tests.support.rigs.machine_execution_rig import canned as canned
from tests.support.rigs.machine_execution_rig import model as model

pytestmark = pytest.mark.integration


async def test_two_serial_steps_share_one_machine(
    fake_frames: Any, silent_dispatcher: None, canned: list[str]
) -> None:
    task_id, (first, second) = await task_with_steps(2)
    for step_id in (first, second):
        await step_run(task_id, step_id)
    assert len(fake_frames.created) == 1, "one Machine frame for both steps"
    assert canned[1] == "/work/step-1.md", "the second step sees the first step's /work"
    row = await step_machine(task_id)
    assert row is not None and row.state == MachineState.IDLE and row.held_by is None
    assert list(fake_frames.world) == [row.frame_ref], "an idle Machine keeps its frame"


async def test_a_later_step_in_the_machine_reads_only_its_own_inputs(
    fake_frames: Any, silent_dispatcher: None, model: Callable[[Run], None]
) -> None:
    """A warm Machine keeps /task across steps, so its boundary empties the mounts before a step
    mounts its own: a step must never read an earlier step's sources - the long-form
    assembly globs /task/inputs, and would assemble the previous turn's sections."""
    mounted: list[list[str]] = []

    async def run(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        mounted.append(sorted(p for p in deps.frame.files if p.startswith("/task/inputs/")))
        name = f"/work/step-{len(mounted)}.md"
        await deps.frame.write_bytes(name, f"# memo {len(mounted)}".encode())  # distinct, never de-duped
        await deps.frame.publish(name, "document", f"memo {len(mounted)}", ())
        return SimpleNamespace(output=f"wrote {name}")

    model(run)
    task_id, (first, second, third) = await task_with_steps(3)
    async with row_scope(Task, task_id) as session:
        session.add_all(
            [
                PlanEdge(task_id=task_id, from_step_id=first, to_step_id=second),
                PlanEdge(task_id=task_id, from_step_id=second, to_step_id=third),
            ]
        )
        await session.commit()
    for step_id in (first, second, third):
        await step_run(task_id, step_id)
    assert len(fake_frames.created) == 1, "three serial steps, one Machine"
    assert mounted == [[], ["/task/inputs/step-1.md"], ["/task/inputs/step-2.md"]]


async def test_a_seat_runs_in_a_throwaway_frame(
    fake_frames: Any, silent_dispatcher: None, canned: list[str]
) -> None:
    task_id, (only,) = await task_with_steps(1)
    await step_run(task_id, only, seat=True)
    assert len(fake_frames.created) == 1
    assert fake_frames.world == {}, "the seat's frame is destroyed with its step"
    assert await step_machine(task_id) is None


async def test_a_seat_by_the_plans_shape_needs_no_dispatch_flag(
    fake_frames: Any, silent_dispatcher: None, canned: list[str]
) -> None:
    task_id, (only,) = await task_with_steps(1)
    async with row_scope(Task, task_id) as session:
        step = await session.get(PlanStep, only)
        assert step is not None
        step.spec = {**step.spec, "model_ref": "anthropic:claude-x"}  # a fleet seat
        await session.commit()
    await step_run(task_id, only)
    assert len(fake_frames.created) == 1 and fake_frames.world == {}
    assert await step_machine(task_id) is None


async def test_the_rollback_lever_gives_every_step_a_throwaway_frame(
    fake_frames: Any, silent_dispatcher: None, canned: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.machines, "enabled", False)
    task_id, (first, second) = await task_with_steps(2)
    for step_id in (first, second):
        await step_run(task_id, step_id)
    assert len(fake_frames.created) == 2, "a fresh frame per step, as before 1b"
    assert canned[1] == "", "the second step does not see the first step's /work"
    assert fake_frames.world == {}
    assert await step_machine(task_id) is None, "no Machine is made"


async def _machine_seconds(task_id: uuid.UUID) -> list[tuple[str, int]]:
    """The task's machine-time rows, oldest first: (model, seconds)."""
    async with row_scope(Task, task_id) as session:
        rows = await session.scalars(
            select(UsageEvent)
            .where(UsageEvent.task_id == task_id, UsageEvent.family == "machine")
            .order_by(UsageEvent.at)
        )
        return [(u.model, int(u.units["second"])) for u in rows.all()]


async def _worked(task_id: uuid.UUID, seconds: int) -> None:
    """The step has run `seconds` on its Machine: move the watermark back that far."""
    async with row_scope(Task, task_id) as session:
        row = await Machine.locked_for_task(session, task_id)
        assert row is not None and row.metered_until is not None
        row.metered_until -= dt.timedelta(seconds=seconds)
        await session.commit()


async def test_a_step_bills_its_machine_time_with_its_outcome(
    fake_frames: Any, silent_dispatcher: None, model: Callable[[Run], None]
) -> None:
    """`_attempt`'s finally bills the segment `release` closed, on the
    step's own session, and it commits with the step - a failed attempt's too (the failure arm
    commits before anything else: money spent is spent)."""
    runs: list[int] = []

    async def run(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        runs.append(len(runs) + 1)
        await _worked(deps.task_id, 120 if len(runs) == 1 else 60)
        if len(runs) == 1:
            return SimpleNamespace(output="done, I think")  # forgot to publish: a failed attempt
        return SimpleNamespace(output=await step_publish(deps.frame, 1))

    model(run)
    task_id, (only,) = await task_with_steps(1)
    await step_run(task_id, only)
    billed = await _machine_seconds(task_id)
    assert [m for m, _ in billed] == ["machine.base", "machine.base"]
    assert 120 <= billed[0][1] < 180 and 60 <= billed[1][1] < 120, "each attempt's own segment, once"


async def test_a_seat_bills_its_throwaway_frame_once(
    fake_frames: Any, silent_dispatcher: None, model: Callable[[Run], None]
) -> None:
    async def run(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(1.05)  # a second of work in the seat's frame
        return SimpleNamespace(output=await step_publish(deps.frame, 1))

    model(run)
    task_id, (only,) = await task_with_steps(1)
    await step_run(task_id, only, seat=True)
    billed = await _machine_seconds(task_id)
    assert len(billed) == 1 and billed[0][0] == "machine.base" and billed[0][1] >= 1
    assert await step_machine(task_id) is None


async def test_a_cancelled_throwaway_park_bills_its_run_once(
    fake_frames: Any, silent_dispatcher: None, model: Callable[[Run], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A throwaway frame (the lever's) bills its run at the park, and the
    cancelled park's teardown by name bills nothing more."""
    monkeypatch.setattr(settings.machines, "enabled", False)
    asking = _asking([], lambda frame: step_publish(frame, 1))

    async def run(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        if kwargs.get("answers") is None:
            await asyncio.sleep(1.05)  # a second of work before the question
        return await asking(agent, prompt, deps, **kwargs)

    model(run)
    task_id, (only,) = await task_with_steps(1)
    assert (await step_run(task_id, only)).startswith(execution.ASKING)
    billed = await _machine_seconds(task_id)
    assert len(billed) == 1 and billed[0][1] >= 1, "the run before the park, billed at the park"
    await _answer(task_id, only, cancelled=True)
    with pytest.raises(PolicyDenied, match="cancelled"):
        await step_run(task_id, only)
    assert fake_frames.world == {}, "the cancelled park's frame is torn down by name"
    assert await _machine_seconds(task_id) == billed, "and bills nothing more"


async def test_a_retry_runs_in_the_same_machine_and_is_told_what_changed(
    fake_frames: Any, silent_dispatcher: None, model: Callable[[Run], None]
) -> None:
    prompts: list[str] = []

    async def flaky(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        prompts.append(str(prompt))
        if len(prompts) == 1:  # the first attempt writes a draft and forgets to publish it
            await deps.frame.write_bytes("/work/draft.md", b"half")
            return SimpleNamespace(output="done, I think")
        return SimpleNamespace(output=await step_publish(deps.frame, 1))

    model(flaky)
    task_id, (only,) = await task_with_steps(1)
    await step_run(task_id, only)
    assert len(prompts) == 2 and len(fake_frames.created) == 1, "the retry ran in the same Machine"
    assert "This is attempt 2, in the same Machine" not in prompts[0]
    assert "This is attempt 2, in the same Machine" in prompts[1]
    assert "- /work/draft.md" in prompts[1], "the retry is shown what the failed attempt changed"


def _asking(answers: list[Any], then: Callable[[Any], Awaitable[Any]]) -> Run:
    """First run: park on one ask_user question. The resume: record the answer, then `then`."""

    async def run(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        if kwargs.get("answers") is None:
            call = ToolCallPart(
                tool_name=ASK_USER,
                args={"questions": [{"question": "Which colour?", "options": ["blue", "red"]}]},
                tool_call_id="ask-1",
            )
            return SimpleNamespace(
                output=DeferredToolRequests(calls=[call]),
                all_messages=lambda: [ModelResponse(parts=[call])],
            )
        answers.append(kwargs["answers"].calls["ask-1"])
        return SimpleNamespace(output=await then(deps.frame))

    return run


async def _answer(task_id: uuid.UUID, step_id: uuid.UUID, **resolution: Any) -> None:
    async with row_scope(Task, task_id) as session:
        step = await session.get(PlanStep, step_id)
        assert step is not None and step.resume_state
        step.resume_state = {**step.resume_state, "answer": "blue", "cancelled": False, **resolution}
        await session.commit()


async def test_a_park_keeps_the_machine_and_the_resume_binds_it(
    fake_frames: Any, silent_dispatcher: None, model: Callable[[Run], None]
) -> None:
    answers: list[Any] = []
    model(_asking(answers, lambda frame: step_publish(frame, 1)))
    task_id, (only,) = await task_with_steps(1)

    assert (await step_run(task_id, only)).startswith(execution.ASKING)
    parked = await step_machine(task_id)
    assert parked is not None and parked.state == MachineState.PARKED
    assert parked.held_by is not None and parked.held_by.startswith(f"step:{only}:")

    await _answer(task_id, only)
    assert (await step_run(task_id, only)) == "wrote /work/step-1.md"
    assert answers == ["blue"], "the resume is told the answer, and nothing about a reset"
    assert len(fake_frames.created) == 1, "the resume bound the parked Machine"
    row = await step_machine(task_id)
    assert row is not None and row.state == MachineState.IDLE and row.held_by is None


async def test_a_cancelled_park_gives_the_machine_back(
    fake_frames: Any, silent_dispatcher: None, model: Callable[[Run], None]
) -> None:
    model(_asking([], lambda frame: step_publish(frame, 1)))
    task_id, (only,) = await task_with_steps(1)
    assert (await step_run(task_id, only)).startswith(execution.ASKING)

    await _answer(task_id, only, cancelled=True)
    with pytest.raises(PolicyDenied, match="cancelled"):
        await step_run(task_id, only)
    row = await step_machine(task_id)
    assert row is not None and row.state == MachineState.IDLE and row.held_by is None
    assert list(fake_frames.world) == [row.frame_ref], "an idle Machine keeps its frame"


async def test_the_workflows_cancel_exit_gives_any_park_back(
    fake_frames: Any, silent_dispatcher: None, model: Callable[[Run], None]
) -> None:
    """A park cancelled through the step workflow's exit (a gate resolved `cancel`, the
    task's header cancel) hands its Machine back - every step's, not only a browser step's -
    or the task's next turn would find its Machine held by a step that will never resume."""
    model(_asking([], lambda frame: step_publish(frame, 1)))
    task_id, (only,) = await task_with_steps(1)
    assert (await step_run(task_id, only)).startswith(execution.ASKING)
    async with row_scope(Task, task_id) as session:
        await execution.close_cancelled_park(session, task_id, only)
    row = await step_machine(task_id)
    assert row is not None and row.state == MachineState.IDLE and row.held_by is None


async def test_a_browser_park_that_slept_is_told_the_page_was_reset(
    fake_frames: Any, silent_dispatcher: None, model: Callable[[Run], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.computer_use, "enabled", True)
    answers: list[Any] = []
    model(_asking(answers, lambda frame: step_publish(frame, 1)))
    task_id, (only,) = await task_with_steps(1, agent="operator", browser=True)
    assert (await step_run(task_id, only)).startswith(execution.ASKING)

    parked = await step_machine(task_id)
    assert parked is not None and parked.state == MachineState.PARKED
    assert await machines.sleep(parked.id, parked.epoch, SleepReason.PARK_HOLD) == "slept"

    await _answer(task_id, only)
    await step_run(task_id, only)
    assert answers == [f"{PAGE_RESET}\n\nblue"]
    assert len(fake_frames.created) == 2, "the park's Machine woke in a new frame"
    row = await step_machine(task_id)
    assert row is not None and row.state == MachineState.IDLE and row.epoch == parked.epoch + 1


async def test_every_browser_step_in_a_warm_machine_publishes_its_own_recording(
    fake_frames: Any, silent_dispatcher: None, model: Callable[[Run], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sidecar outlives its step in a warm Machine and reads its recording folder once, when it
    starts: without the step boundary moving it, the second step's acts land in the first step's
    folder and the second step's own recording is never published."""
    monkeypatch.setattr(settings.computer_use, "enabled", True)
    runs: list[int] = []

    async def browse(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        runs.append(len(runs) + 1)
        await deps.frame.browser({"verb": "click", "ref": "e1.1"})
        return SimpleNamespace(output=await step_publish(deps.frame, len(runs)))

    model(browse)
    task_id, (first, second) = await task_with_steps(2, agent="operator", browser=True)
    for step_id in (first, second):
        await step_run(task_id, step_id)
    assert len(fake_frames.created) == 1, "both steps ran in one warm Machine"
    async with row_scope(Task, task_id) as session:
        recorded = [a.step_id for a in await Artifact.for_task(session, task_id) if a.type == "recording"]
    assert len(recorded) == 2 and set(recorded) == {first, second}, "one recording per step"


async def test_a_park_from_before_1b_resumes_on_its_old_frame(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """No Machine row holds the step; its frame is frame-<task>-<step>-<attempt>."""
    task_id, (step_id,) = await task_with_steps(1)
    legacy = fake_frames(f"{task_id}-{step_id}-1")
    await legacy.create()
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        step = await session.get(PlanStep, step_id)
        assert task is not None and step is not None
        step.attempt = 1
        acquired = await ex_acquire.acquire_for(
            session, task, step, profile="base", allow_domains=[], resume={"history": ""}, seat=False
        )
        assert acquired.kind == "legacy" and acquired.frame.ref == legacy.ref
        segment = await machines.release(acquired, parked=False)
    assert fake_frames.world == {}
    assert segment is None or (segment.task_id == task_id and segment.at is None), "billed like a throwaway"
    assert await step_machine(task_id) is None, "a pre-1b park never makes a Machine"


async def test_a_park_in_the_machine_resumes_there_after_the_lever_is_pulled(
    fake_frames: Any, silent_dispatcher: None, model: Callable[[Run], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lever hands NEW steps throwaway frames; a run parked in the Machine lives
    there, so its resume binds it - and gives it back when it is done, never stranding the hold."""
    answers: list[Any] = []
    model(_asking(answers, lambda frame: step_publish(frame, 1)))
    task_id, (only,) = await task_with_steps(1)
    assert (await step_run(task_id, only)).startswith(execution.ASKING)

    monkeypatch.setattr(settings.machines, "enabled", False)
    await _answer(task_id, only)
    assert (await step_run(task_id, only)) == "wrote /work/step-1.md"
    assert answers == ["blue"] and len(fake_frames.created) == 1, "the resume bound the parked Machine"
    row = await step_machine(task_id)
    assert row is not None and row.state == MachineState.IDLE and row.held_by is None


async def test_a_throwaway_park_whose_frame_is_gone_costs_a_rung_not_the_task(
    fake_frames: Any, silent_dispatcher: None, model: Callable[[Run], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A park no Machine holds (the lever's, a seat's, or one from before 1b) resumes on its frame
    by name; when that frame is gone the attempt fails as INFRA and the next one starts fresh."""
    monkeypatch.setattr(settings.machines, "enabled", False)
    model(_asking([], lambda frame: step_publish(frame, 1)))
    task_id, (only,) = await task_with_steps(1)
    assert (await step_run(task_id, only)).startswith(execution.ASKING)
    fake_frames.world.clear()  # a reaper took the parked frame

    await _answer(task_id, only)
    assert (await step_run(task_id, only)).startswith(execution.ASKING), "the fresh attempt asks again"
    async with row_scope(Task, task_id) as session:
        step = await session.get(PlanStep, only)
        assert step is not None and step.attempt == 2 and step.infra_attempts == 1


async def _parked(
    model: Callable[[Run], None], *, agent: str = "maker", browser: bool = False
) -> tuple[uuid.UUID, uuid.UUID]:
    """A one-step task whose step parked on a question, holding its Machine."""
    model(_asking([], lambda frame: step_publish(frame, 1)))
    task_id, (only,) = await task_with_steps(1, agent=agent, browser=browser)
    assert (await step_run(task_id, only)).startswith(execution.ASKING)
    parked = await step_machine(task_id)
    assert parked is not None and parked.state == MachineState.PARKED
    return task_id, only


async def _given_back(task_id: uuid.UUID) -> None:
    row = await step_machine(task_id)
    assert row is not None and row.state == MachineState.IDLE and row.held_by is None


async def test_a_finished_tasks_park_holds_nothing(
    fake_frames: Any, silent_dispatcher: None, model: Callable[[Run], None]
) -> None:
    """I1(a): a worker restart while parked, then a header cancel before recovery - the recovered
    step meets a finished task at the door, and its park must not outlive it."""
    task_id, only = await _parked(model)
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        assert task is not None
        task.transition(TaskStatus.CANCELLED)
        await session.commit()
    assert (await step_run(task_id, only)).startswith("(the task ended")
    await _given_back(task_id)


async def test_a_settled_twins_park_holds_nothing(
    fake_frames: Any, silent_dispatcher: None, model: Callable[[Run], None]
) -> None:
    """I1(b): the twin guard's exit gives back a park its step still holds."""
    task_id, only = await _parked(model)
    async with row_scope(Task, task_id) as session:
        step = await session.get(PlanStep, only)
        assert step is not None
        step.state = StepState.DONE.value
        await session.commit()
    assert (await step_run(task_id, only)).startswith("(already settled")
    await _given_back(task_id)


@pytest.mark.parametrize("exit_", ["terminal", "twin"])
async def test_a_park_that_cannot_be_given_back_never_fails_the_exit(
    fake_frames: Any,
    silent_dispatcher: None,
    model: Callable[[Run], None],
    monkeypatch: pytest.MonkeyPatch,
    exit_: str,
) -> None:
    """The terminal-task door and the twin guard must return normally. A park
    that cannot be given back there is logged and left to the reconciler (it releases any dead
    hold), never raised out of the exit."""
    task_id, only = await _parked(model)
    async with row_scope(Task, task_id) as session:
        if exit_ == "terminal":
            task = await session.get(Task, task_id)
            assert task is not None
            task.transition(TaskStatus.CANCELLED)
        else:
            step = await session.get(PlanStep, only)
            assert step is not None
            step.state = StepState.DONE.value
        await session.commit()

    async def broken(*args: Any, **kwargs: Any) -> bool:
        raise ConnectionError("the database went away")

    monkeypatch.setattr(machines, "holds", broken)
    said = await step_run(task_id, only)
    assert said.startswith("(the task ended" if exit_ == "terminal" else "(already settled")


async def test_a_gate_that_cannot_open_gives_the_park_back(
    fake_frames: Any, silent_dispatcher: None, model: Callable[[Run], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """I1(c): `_attempt` already released the Machine as parked; nothing will resume it."""

    async def broken(*args: Any, **kwargs: Any) -> uuid.UUID:
        raise RuntimeError("the gate could not be opened")

    monkeypatch.setattr(ex_parks, "park_on_question", broken)
    model(_asking([], lambda frame: step_publish(frame, 1)))
    task_id, (only,) = await task_with_steps(1)
    with pytest.raises(RuntimeError, match="could not be opened"):
        await step_run(task_id, only)
    await _given_back(task_id)


async def test_a_park_is_given_back_even_when_the_step_session_cannot_commit(
    fake_frames: Any, silent_dispatcher: None, model: Callable[[Run], None]
) -> None:
    """M1: the ids are read first; a failed commit rolls back (expiring the rows) and the park is
    still given back, on `release_parked`'s own session."""
    task_id, only = await _parked(model)
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        step = await session.get(PlanStep, only)
        assert task is not None and step is not None

        class _Lost:  # the step session, its connection gone
            async def commit(self) -> None:
                raise ConnectionError("the connection was lost")

            async def rollback(self) -> None:
                await session.rollback()

        await ex_giveback.discard_parked_frame(cast(AsyncSession, _Lost()), task, step)
    await _given_back(task_id)


async def test_the_kill_switch_under_a_machine_park_gives_it_back(
    fake_frames: Any, silent_dispatcher: None, model: Callable[[Run], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: the resume's early-except path (computer use switched off while the browser step
    waited) gives the Machine back before the ladder moves on."""
    monkeypatch.setattr(settings.computer_use, "enabled", True)
    task_id, only = await _parked(model, agent="operator", browser=True)
    monkeypatch.setattr(settings.computer_use, "enabled", False)
    await _answer(task_id, only)
    with pytest.raises(StepFailed):
        await step_run(task_id, only)
    await _given_back(task_id)


async def test_a_cancel_right_after_acquire_still_gives_the_machine_back(
    fake_frames: Any, silent_dispatcher: None, canned: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4: a cancellation that lands between `acquire` returning and the step's `try` (the
    progress row's close) is still covered by the `finally` that releases."""
    from prismoid.operations import live

    real = live.activity_emitter

    def emitter(wire: Any, **kwargs: Any) -> Any:
        fact = real(wire, **kwargs)

        async def cancelled_on_close(phase: Any, label: str, *, started: float | None = None) -> None:
            if phase == "end" and label == "Starting the Machine":
                raise asyncio.CancelledError
            await fact(phase, label, started=started)

        return cancelled_on_close

    monkeypatch.setattr(live, "activity_emitter", emitter)
    task_id, (only,) = await task_with_steps(1)
    with pytest.raises(asyncio.CancelledError):
        await step_run(task_id, only)
    await _given_back(task_id)


async def test_a_cancel_ends_a_ladder_whose_machine_fails_at_once(
    fake_frames: Any, silent_dispatcher: None, canned: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An infra rung nets out of the ladder's cap, so a Machine that fails at once on
    every attempt retries until the task is cancelled - and the cancel must end it: the next rung
    finds the task ended and buys nothing. (A prepare failure spun 4,000 rungs in ten
    minutes after the cancel and exhausted Postgres's connections.) The failure here comes after
    acquire's own `wait_s`, so no pace is owed and the next rung runs in the same ladder call."""
    from prismoid.errors import ModelIncapable

    monkeypatch.setattr(settings.machines, "wait_s", 1)
    task_id, (only,) = await task_with_steps(1)
    calls = 0

    async def failing(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        await asyncio.sleep(1.05)  # acquire waited its `wait_s` out before it gave up
        if calls == 1:  # the user cancels while the first attempt fails
            async with row_scope(Task, task_id) as session:
                await execution.advance(session, task_id, TaskStatus.CANCELLED)
                await session.commit()
        if calls > 3:
            pytest.fail("the ladder kept acquiring a Machine for a cancelled task")
        raise ModelIncapable("the Machine could not be prepared: boom", infra=True)

    monkeypatch.setattr(machines, "acquire", failing)
    outcome = await step_run(task_id, only)
    assert calls == 1 and "ended" in outcome
    async with row_scope(Task, task_id) as session:
        step = await session.get(PlanStep, only)
        assert step is not None and step.attempt == 1 and step.infra_attempts == 1


async def test_a_machine_that_fails_at_once_waits_out_wait_s_before_the_next_rung(
    fake_frames: Any, silent_dispatcher: None, canned: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step that cannot get its Machine retries every `wait_s` until it is cancelled. A
    rung whose Machine failed at once hands the workflow the rest of `wait_s` to wait out durably,
    and leaves the step `ready`, holding nothing, for the next rung. (Unpaced, one step
    spun ~35 rungs a second and saturated the shared Postgres.)"""
    from prismoid.errors import ModelIncapable

    monkeypatch.setattr(settings.machines, "wait_s", 600)
    real = machines.acquire
    calls = 0

    async def fails_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ModelIncapable("the Machine could not be prepared: boom", infra=True)
        return await real(*args, **kwargs)

    monkeypatch.setattr(machines, "acquire", fails_once)
    task_id, (only,) = await task_with_steps(1)
    outcome = await step_run(task_id, only)
    assert calls == 1 and outcome.startswith(execution.PACING)
    assert 590 <= float(outcome.removeprefix(execution.PACING)) <= 600
    async with row_scope(Task, task_id) as session:
        step = await session.get(PlanStep, only)
        assert step is not None and step.step_state is StepState.READY and step.model_attempts() == 0
    # a worker restart replays the workflow's body before its recorded sleep: the door re-paces
    # with what is left of the wait instead of buying the next rung early
    replayed = await step_run(task_id, only, handed_back=0)
    assert calls == 1 and replayed.startswith(execution.PACING)
    assert float(replayed.removeprefix(execution.PACING)) <= float(outcome.removeprefix(execution.PACING))
    # the wait is over: the workflow runs the ladder again, and the next rung is a fresh attempt
    later = execution.utcnow() + dt.timedelta(seconds=601)
    monkeypatch.setattr(execution, "utcnow", lambda: later)
    assert (await step_run(task_id, only, handed_back=0)).startswith(execution.PACING), (
        "a replay re-issues the recorded pace even past its deadline: DBOS's sleep is its own clock"
    )
    outcome = await step_run(task_id, only, handed_back=1)
    assert calls == 2 and not outcome.startswith(execution.PACING)
    async with row_scope(Task, task_id) as session:
        step = await session.get(PlanStep, only)
        assert step is not None and step.step_state is StepState.DONE
        assert step.attempt == 2 and step.model_attempts() == 1


async def test_a_cancel_during_the_wait_ends_the_ladder_at_its_door(
    fake_frames: Any, silent_dispatcher: None, canned: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The paced wait is the workflow's, so a cancel lands while no rung runs: the workflow's next
    call meets the ended task at the step's door and acquires nothing more."""
    from prismoid.errors import ModelIncapable

    calls = 0

    async def failing(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise ModelIncapable("the Machine could not start: boom", infra=True)

    monkeypatch.setattr(machines, "acquire", failing)
    task_id, (only,) = await task_with_steps(1)
    assert (await step_run(task_id, only)).startswith(execution.PACING)
    async with row_scope(Task, task_id) as session:  # the user cancels during the wait
        await execution.advance(session, task_id, TaskStatus.CANCELLED)
        await session.commit()
    assert "ended" in await step_run(task_id, only) and calls == 1


async def test_an_agents_own_infra_stop_is_not_paced(
    fake_frames: Any, silent_dispatcher: None, model: Callable[[Run], None]
) -> None:
    """Pacing is the Machine's: a rung the agent itself lost to infra (its wall clock) retries at
    once, in the same ladder call - it already ran for its whole wall clock."""
    from prismoid.errors import ModelIncapable

    calls = 0

    async def run(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ModelIncapable("the step ran past its wall clock", infra=True)
        return SimpleNamespace(output=await step_publish(deps.frame, calls))

    model(run)
    task_id, (only,) = await task_with_steps(1)
    outcome = await step_run(task_id, only)
    assert calls == 2 and not outcome.startswith(execution.PACING)


async def test_the_acquire_phases_name_the_machine(
    fake_frames: Any, silent_dispatcher: None, canned: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step that binds a warm Machine moves it idle -> running and no machine.* event says so:
    the acquire's progress facts carry `tool="machine"`, the signal a client re-reads the chip on.
    A seat's throwaway frame is not the Machine, so its facts stay the engine's."""
    from prismoid.operations import live

    real = live.activity_emitter
    facts: list[tuple[str, str, str]] = []

    def emitter(wire: Any, **kwargs: Any) -> Any:
        fact = real(wire, **kwargs)

        async def spy(phase: Any, label: str, *, started: float | None = None) -> None:
            facts.append((kwargs.get("tool", ""), phase, label))
            await fact(phase, label, started=started)

        return spy

    monkeypatch.setattr(live, "activity_emitter", emitter)
    task_id, (first, second, seat) = await task_with_steps(3)
    for step in (first, second):
        await step_run(task_id, step)
    await step_run(task_id, seat, seat=True)
    named = [(phase, label) for tool, phase, label in facts if tool == "machine"]
    assert ("end", "Starting the Machine") in named  # the first step creates it
    assert ("end", "Preparing the Machine") in named  # the second binds it warm
    assert all(label != "Starting a sandbox" for _, label in named)  # the seat's frame is not it
