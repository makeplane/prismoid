# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The routine contract with the Lead: an unattended run ends in a result for its owner, a typed failure
or "needs the owner", never in a question nobody sees; a Lead's ask with defaults proceeds on timeout.

The real `execute_step` on a turn's express step seated with the Lead (the cutover's mint, arranged by
hand), on a task a routine minted. The owner hears a run that found something, a quiet run stays quiet,
and a failed run fails, counts on the routine's ladder and reaches the owner's inbox.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from sqlalchemy.ext.asyncio import AsyncSession

import prismoid.ai.agents.base as agents_base
from prismoid.ai.deps import TaskDeps
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.gate import Gate
from prismoid.db.models.inbox import InboxItem, InboxKind
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.routine import Routine
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.errors import StepFailed
from prismoid.operations import execution, gates, planning
from prismoid.operations import routines as routine_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations.execution import lead_rung
from prismoid.schemas.events import StepStateChanged
from prismoid.schemas.lead import AskUser, Deliver, Fail, FailKind, LeadEndingKind
from prismoid.schemas.plan import (
    GateKind,
    GateSpec,
    ProposedQuestion,
    QuestionType,
    StepState,
    TaskStatus,
    TimeoutPolicy,
)
from tests.support.cast import member
from tests.support.dispatchers import RecordingDispatcher
from tests.support.runners import scripted

pytestmark = pytest.mark.integration

_TIME = AskUser(
    context="The time depends on the city.",
    questions=[ProposedQuestion(prompt="Which city?", type=QuestionType.TEXT, default="UTC")],
)


async def _lead_turn(
    monkeypatch: pytest.MonkeyPatch, *, routine: bool
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A turn whose express step the Lead holds, running, on a routine's task or a person's: (task, step, owner)."""

    async with session_scope() as session:
        owner, _ = await member(session, "Owner")
        await session.flush()
        task = await task_ops.create_task(session, owner, goal="What's the time?", clarify="auto")
        task.transition(TaskStatus.PLANNING)
        await session.flush()
        view = await planning.draft_plan(session, task.id)
        step = await session.get(PlanStep, uuid.UUID(str(view.steps[0].id)))
        assert step is not None and step.spec["lane_role"] == "express"
        step.spec = {**step.spec, "agent": lead_rung.LEAD}
        if routine:
            schedule = {"cron": "0 9 * * 1-5", "every_s": None, "at": None, "timezone": "UTC"}
            made = await routine_ops.create_routine(
                session,
                owner,
                name=f"digest-{uuid.uuid4().hex[:8]}",
                instruction="the digest",
                schedule=schedule,
            )
            task.origin_routine_id = made.id
        await session.commit()
        ids = task.id, step.id, owner.id
    async with row_scope(Task, ids[0]) as session:
        await execution.advance(session, ids[0], TaskStatus.RUNNING)
        await session.commit()
    return ids


def _lead_says(monkeypatch: pytest.MonkeyPatch, *outputs: object) -> list[str]:
    """The Lead's runs end in `outputs`, one per run, with no model; the prompt each run read."""
    prompts: list[str] = []

    async def run(agent: object, prompt: str, deps: TaskDeps, **kwargs: object) -> object:
        prompts.append(prompt)
        return SimpleNamespace(output=outputs[len(prompts) - 1])

    monkeypatch.setattr(agents_base, "build", scripted(run))
    return prompts


def _model_says(monkeypatch: pytest.MonkeyPatch, *turns: tuple[str, dict[str, object] | None]) -> None:
    """The real Lead, its model answering `turns` in order: `(text, None)` or `(output tool, args)`."""
    queue = list(turns)

    def response(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        said, args = queue.pop(0)
        return ModelResponse(parts=[TextPart(said) if args is None else ToolCallPart(said, args)])

    async def stream(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        said, args = queue.pop(0)
        if args is None:
            yield said
        else:
            yield {0: DeltaToolCall(name=said, json_args=json.dumps(args))}

    model = FunctionModel(response, stream_function=stream)
    monkeypatch.setattr(agents_base, "resolve", lambda *a, **k: model)


async def _step_run(task_id: uuid.UUID, step_id: uuid.UUID) -> str:
    async with row_scope(Task, task_id) as session:
        return await execution.execute_step(session, task_id, step_id)


async def _frames(task_id: uuid.UUID, step_id: uuid.UUID) -> list[StepStateChanged]:
    async with row_scope(Task, task_id) as session:
        events = await TaskEvent.of_types(session, task_id, ("step.state",))
        return [e for e in events if isinstance(e, StepStateChanged) and e.step_id == step_id]


@dataclass(frozen=True, slots=True)
class Settled:
    """A settled run, read back: the task's status, its routine's failure count and the owner's knocks for it."""

    status: TaskStatus
    failures: int
    knocks: list[tuple[str, str]]  # (inbox kind, body)


async def _settle(task_id: uuid.UUID, said: str) -> Settled:
    """The turn's delivery, as `run_task` delivers it."""
    async with row_scope(Task, task_id) as session:
        await execution.deliver(session, task_id, said)
        await session.commit()
        return await _settled(session, task_id)


async def _settled(session: AsyncSession, task_id: uuid.UUID) -> Settled:
    task = await session.get(Task, task_id, populate_existing=True)
    assert task is not None and task.origin_routine_id is not None
    routine = await session.get(Routine, task.origin_routine_id, populate_existing=True)
    owner = await session.get(Actor, task.creator_actor_id)
    assert routine is not None and owner is not None
    inbox = await InboxItem.list_for(session, owner)
    knocks = [(i.kind, i.body) for i in inbox if i.task_id == task_id]
    return Settled(task.state, routine.consecutive_failures, knocks)


async def test_a_routine_turn_never_delivers_a_question(monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, step_id, _ = await _lead_turn(monkeypatch, routine=True)
    found: dict[str, object] = {"result": "Two alerts since 09:00.", "findings": True}
    _model_says(
        monkeypatch,
        ("ask_person", _TIME.model_dump(mode="json", exclude={"ending"})),
        ("Which city do you mean?", None),
        ("deliver_to_owner", found),
    )
    said = await _step_run(task_id, step_id)
    assert said == "Two alerts since 09:00.", "the question and the free text went back; the result landed"
    (done,) = [f for f in await _frames(task_id, step_id) if f.state is StepState.DONE]
    assert done.ending is LeadEndingKind.DELIVER and done.findings is True
    async with row_scope(Task, task_id) as session:
        assert await Gate.count_for_step(session, step_id) == 0, "nobody was asked"


@pytest.mark.parametrize(("findings", "speaks"), [(True, True), (False, False)])
async def test_a_finding_reaches_the_owner_and_a_quiet_run_stays_quiet(
    monkeypatch: pytest.MonkeyPatch, findings: bool, speaks: bool
) -> None:
    task_id, step_id, _ = await _lead_turn(monkeypatch, routine=True)
    result = "Two alerts since 09:00." if findings else "Nothing new since 09:00."
    _lead_says(monkeypatch, Deliver(result=result, findings=findings))
    said = await _step_run(task_id, step_id)
    settled = await _settle(task_id, said)
    assert settled.status is TaskStatus.DELIVERED and settled.failures == 0
    pings = [body for kind, body in settled.knocks if kind == InboxKind.ROUTINE_RUN.value]
    assert pings == ([result] if speaks else [])


async def test_a_typed_failure_fails_the_run_counts_and_reaches_the_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, step_id, _ = await _lead_turn(monkeypatch, routine=True)
    _lead_says(monkeypatch, Fail(kind=FailKind.NEEDS_OWNER, reason="The mailbox is not connected."))
    with pytest.raises(StepFailed) as stopped:
        await _step_run(task_id, step_id)
    async with row_scope(Task, task_id) as session:
        await execution.fail(session, task_id, stopped.value.message, turn=1)  # as run_task fails it
        await session.commit()
        settled = await _settled(session, task_id)
        assert await Gate.count_for_step(session, step_id) == 0, "nobody was asked"
    assert settled.status is TaskStatus.FAILED and settled.failures == 1
    ((kind, body),) = settled.knocks
    assert kind == InboxKind.ESCALATION.value and "The mailbox is not connected." in body
    (stop,) = [f for f in await _frames(task_id, step_id) if f.state is StepState.ESCALATED]
    assert stop.ending is LeadEndingKind.FAIL


async def test_a_lead_ask_with_defaults_proceeds_on_timeout_with_those_defaults(
    monkeypatch: pytest.MonkeyPatch, recording_dispatcher: RecordingDispatcher
) -> None:
    task_id, step_id, _ = await _lead_turn(monkeypatch, routine=False)
    prompts = _lead_says(monkeypatch, _TIME, "It is 14:02 in UTC.")
    gate_id = uuid.UUID((await _step_run(task_id, step_id)).removeprefix(execution.ASKING))
    async with row_scope(Task, task_id) as session:
        gate = await session.get(Gate, gate_id)
        assert gate is not None and gate.kind == GateKind.INPUT.value and gate.expires_at is not None
        asked = GateSpec.model_validate(gate.spec).questions
        assert asked is not None and asked.on_timeout is TimeoutPolicy.PROCEED_WITH_DEFAULTS
        assert asked.questions[0].default == "UTC" and asked.questions[0].required is False
        gate.expires_at = utcnow()  # nobody answered within the TTL
        await session.commit()
        assert await gates.sweep(session) == [gate_id]
    (wake,) = [p for k, p, _ in recording_dispatcher.sends if k == f"step-{step_id}"]
    async with row_scope(Task, task_id) as session:
        await execution.store_answer(session, step_id, cast("dict[str, object]", wake))
        await session.commit()
    assert await _step_run(task_id, step_id) == "It is 14:02 in UTC."
    assert "**Asked:** Which city? **Default used:** UTC" in prompts[1], "the fresh run reads the default"
