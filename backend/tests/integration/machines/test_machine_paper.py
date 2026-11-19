# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A serial step starts on paper (ADR-063, amended): the real `execute_step` with the model stubbed
delivers a writer's memo through the one publish door into the store, with no Machine row, no frame
made and no machine time billed. A step whose shell runs, and the switch off, take the Machine. The rig is
`tests/support/rigs/machine_execution_rig.py`."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai import DeferredToolRequests
from pydantic_ai.messages import ModelResponse, ToolCallPart
from sqlalchemy import text

from prismoid.ai.tools.ask import ASK_USER
from prismoid.conf import settings
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.errors import PolicyDenied
from prismoid.operations import execution, live, machines
from prismoid.operations.execution import acquire
from prismoid.operations.execution.handlers import rung
from prismoid.operations.execution.handlers.state import Ladder
from prismoid.schemas.contract import Contract
from prismoid.schemas.ids import StepId, TaskId
from prismoid.schemas.outputs import SubmittedDocument
from prismoid.schemas.plan import StepSpec, StepState
from tests.support.rigs.machine_execution_rig import canned as canned
from tests.support.rigs.machine_execution_rig import model as model
from tests.support.rigs.machine_execution_rig import step_machine, step_run, task_with_steps

pytestmark = pytest.mark.integration

MEMO = SubmittedDocument(title="Q3 renewals", content="The pipeline grew by a third.")


@pytest.fixture(autouse=True)
def paper_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.machines, "enabled", True)
    monkeypatch.setattr(settings.machines, "paper", True)


@pytest.fixture
def writes_a_memo(model: Any) -> list[Any]:
    """The writer's run hands back its typed document; the system publishes it. Records the frame it ran on."""
    frames: list[Any] = []

    async def run(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        frames.append(deps.frame)
        return SimpleNamespace(output=MEMO, all_messages=list)

    model(run)
    return frames


def _on_paper(frame: Any) -> bool:
    """The step's frame is still its paper (`OnDemandFrame` before a tool took the Machine)."""
    return bool(getattr(frame, "on_paper", False))


async def _machine_seconds(task_id: Any) -> int:
    async with row_scope(Task, task_id) as session:
        found = await session.scalar(
            text("select count(*) from usage_events where task_id = :t and family = 'machine'"),
            {"t": task_id},
        )
    return int(found or 0)


async def test_a_writer_step_delivers_on_paper_with_no_machine(
    fake_frames: Any, writes_a_memo: list[Any]
) -> None:
    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    await step_run(task_id, step_id)

    assert [_on_paper(f) for f in writes_a_memo] == [True]
    assert await step_machine(task_id) is None, "no Machine row"
    assert fake_frames.made == [], "no frame was made on any driver"
    assert await _machine_seconds(task_id) == 0, "no machine time billed"
    async with row_scope(Task, task_id) as session:
        (artifact,) = await Artifact.for_step(session, step_id)
        step = await session.get(PlanStep, step_id)
        assert artifact.title == "Q3 renewals" and artifact.bytes == len(MEMO.markdown.encode())
        assert step is not None and step.step_state is StepState.DONE


async def test_a_step_that_needs_the_computer_still_takes_the_machine(
    fake_frames: Any, canned: list[str]
) -> None:
    task_id, (step_id,) = await task_with_steps(1, agent="maker")
    await step_run(task_id, step_id)
    assert await step_machine(task_id) is not None


async def test_the_switch_off_puts_a_writer_back_on_the_machine(
    fake_frames: Any, writes_a_memo: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.machines, "paper", False)
    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    await step_run(task_id, step_id)
    assert [_on_paper(f) for f in writes_a_memo] == [False]
    assert await step_machine(task_id) is not None


async def test_a_resume_nothing_holds_gets_a_fresh_paper_frame(fake_frames: Any) -> None:
    """A writer that parked to ask resumes on a new paper frame: its inputs are mounted again, as on
    every frame, and it never wrote a file of its own."""
    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    wire = live.Wire(TaskId(task_id), StepId(step_id), None)
    fact = live.activity_emitter(wire)

    async def claim(resume: dict[str, object]) -> machines.Acquired:
        async with row_scope(Task, task_id) as session:
            task, step = await session.get(Task, task_id), await session.get(PlanStep, step_id)
            assert task is not None and step is not None
            phases = acquire.phases_for(wire, fact, seat=False, paper=True)
            return await acquire.acquire(
                session, task, step, profile="base", allowed=set(), resume=resume, seat=False, phases=phases
            )

    assert (await claim({"told": 0})).kind == "paper", "nothing holds this step"


def _asks_then_writes(answers: list[str]) -> Any:
    """The writer parks on one ask_user question; resumed, it records the answer and hands back its document."""

    async def run(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        if kwargs.get("answers") is None:
            call = ToolCallPart(
                tool_name=ASK_USER, args={"questions": [{"question": "Tone?"}]}, tool_call_id="ask-1"
            )
            return SimpleNamespace(
                output=DeferredToolRequests(calls=[call]), all_messages=lambda: [ModelResponse(parts=[call])]
            )
        answers.append(kwargs["answers"].calls["ask-1"])
        assert _on_paper(deps.frame), "the resume runs on paper too"
        return SimpleNamespace(output=MEMO, all_messages=list)

    return run


async def _answer(task_id: Any, step_id: Any, **resolution: Any) -> None:
    async with row_scope(Task, task_id) as session:
        step = await session.get(PlanStep, step_id)
        assert step is not None and step.resume_state
        step.resume_state = {**step.resume_state, "answer": "plain", "cancelled": False, **resolution}
        await session.commit()


async def test_a_writer_that_asks_resumes_on_paper_and_delivers(
    fake_frames: Any, silent_dispatcher: None, model: Any
) -> None:
    answers: list[str] = []
    model(_asks_then_writes(answers))
    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    assert (await step_run(task_id, step_id)).startswith(execution.ASKING)
    assert await step_machine(task_id) is None, "a park on paper holds no Machine"

    await _answer(task_id, step_id)
    await step_run(task_id, step_id)
    assert answers == ["plain"], "the resume is told the answer"
    assert await step_machine(task_id) is None and fake_frames.made == []
    async with row_scope(Task, task_id) as session:
        (artifact,) = await Artifact.for_step(session, step_id)
        assert artifact.title == "Q3 renewals"


async def test_a_cancelled_paper_park_ends_cleanly(
    fake_frames: Any, silent_dispatcher: None, model: Any
) -> None:
    model(_asks_then_writes([]))
    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    assert (await step_run(task_id, step_id)).startswith(execution.ASKING)
    await _answer(task_id, step_id, cancelled=True)
    with pytest.raises(PolicyDenied, match="cancelled"):
        await step_run(task_id, step_id)
    async with row_scope(Task, task_id) as session:
        await execution.close_cancelled_park(session, task_id, step_id)
    # The cancel's hand-back finds no Machine and tears down a pre-1b park by name: a no-op here.
    assert await step_machine(task_id) is None and fake_frames.created == []


async def test_a_step_that_outgrows_paper_finishes_on_the_machine(
    fake_frames: Any, writes_a_memo: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The size check counts what is mounted, not what the step writes: a deliverable past the cap
    fails its paper attempt as infra, and the next rung takes the Machine instead of looping."""
    monkeypatch.setattr(settings.machines, "paper_max_bytes", 16)
    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    await step_run(task_id, step_id)

    assert _on_paper(writes_a_memo[0])
    assert not _on_paper(writes_a_memo[1]), "the retry ran on the Machine"
    assert len(writes_a_memo) == 2, "one paper attempt, then the Machine: never a loop"
    assert await step_machine(task_id) is not None
    async with row_scope(Task, task_id) as session:
        (artifact,) = await Artifact.for_step(session, step_id)
        step = await session.get(PlanStep, step_id)
        assert artifact.title == "Q3 renewals"
        assert step is not None and step.step_state is StepState.DONE and step.infra_attempts == 1


async def test_the_rung_sizes_what_it_mounts_and_remembers_an_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.machines, "paper_max_bytes", 1000)
    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    async with row_scope(Task, task_id) as session:
        task, step = await session.get(Task, task_id), await session.get(PlanStep, step_id)
        assert task is not None and step is not None
        spec = StepSpec.model_validate(step.spec)
        assert spec.contract is not None
        contract: Contract = spec.contract  # declared, so the closure below keeps the narrowing

        def ladder(*upstream_bytes: int) -> Ladder:
            upstream = [Artifact(bytes=n) for n in upstream_bytes]
            return Ladder(session, task, step, spec, contract, seat=False, seated=False, upstream=upstream)

        assert await rung.on_paper(ladder(500), spec)
        assert not await rung.on_paper(ladder(300, 201), spec), "past half the cap: the Machine"
        outgrown = ladder()
        outgrown.outgrew_paper = True
        assert not await rung.on_paper(outgrown, spec), "an attempt that overflowed never goes back"
