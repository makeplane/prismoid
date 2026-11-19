# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Steering a turn the Lead decides (ADR-064, ADR-092): a steer reaches the Lead's run in flight at its safe
points (a call it chose before is held and never runs, an ending is redirected), an ask's answer starts a run
told the steer, and a steer after the Lead planned is taken at the frontier boundary by the Lead's replan seat,
which revises only the steps not started or keeps them.

The real `execute_step` on a turn's express step seated with the Lead, against real rows; the model is a
scripted `FunctionModel` behind the real `build()`, so the real Steering capability, the real replan seat
and the real plan check run."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

import prismoid.ai.agents.base as agents_base
from prismoid.ai.agents.base import definition_for, get
from prismoid.ai.deps import TaskDeps
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.gate import Gate
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.steer import Steer
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.jobs import effects
from prismoid.operations import execution, gates, planning, steering
from prismoid.operations import tasks as task_ops
from prismoid.operations.execution import lead_rung
from prismoid.schemas.events import PlanEdited, SteerApplied, StepNarration, ToolCalled
from prismoid.schemas.ids import TaskId
from prismoid.schemas.lead import SubmitPlan
from prismoid.schemas.plan import (
    DeliverableKind,
    GateAction,
    PlanEditKind,
    PlanProposal,
    ProposedStep,
    StepState,
    TaskStatus,
)
from prismoid.schemas.steering import SafePoint
from tests.support.cast import member
from tests.support.runners import scripted

pytestmark = pytest.mark.integration

Stream = Callable[[list[ModelMessage], AgentInfo], AsyncIterator[str | DeltaToolCalls]]

_PLAN = SubmitPlan(
    say="I'll research the providers, then write the memo.",
    plan=PlanProposal(
        steps=[
            ProposedStep(label="s1", agent="researcher", title="research", goal="Find EU cloud providers"),
            ProposedStep(
                label="s2",
                agent="writer",
                title="write",
                goal="Write the memo",
                depends_on=["s1"],
                publishes=True,
            ),
        ],
        deliverable=DeliverableKind.FILES,
    ),
)


def _revision(agent: str = "writer") -> dict[str, Any]:
    """The replan seat's `revise_plan` arguments: one step, on `agent`, in place of the steps not started."""
    step = {
        "label": "s1",
        "agent": agent,
        "title": "German memo",
        "goal": "Write the memo on German providers",
    }
    return {
        "say": "Narrowing the memo to German providers.",
        "plan": {"steps": [{**step, "publishes": True}], "deliverable": "files"},
    }


_STEER = "Only cover German providers"


def _told(messages: list[ModelMessage]) -> str:
    """Everything the run has been told in words: the prompt, injected steers, enqueued lines."""
    return "\n".join(
        str(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    )


def _call(name: str, args: dict[str, Any], call_id: str) -> DeltaToolCalls:
    return {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=call_id)}


def _model(monkeypatch: pytest.MonkeyPatch, stream: Stream) -> None:
    """The real `build()` with a scripted model: streamed for a step's run, whole for the replan seat."""

    async def whole(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        parts: list[TextPart | ToolCallPart] = []
        async for piece in stream(messages, info):
            if isinstance(piece, str):
                parts.append(TextPart(piece))
            else:
                parts.extend(
                    ToolCallPart(c.name or "", c.json_args, tool_call_id=c.tool_call_id or "x")
                    for c in piece.values()
                )
        return ModelResponse(parts=parts)

    model = FunctionModel(function=whole, stream_function=stream)
    monkeypatch.setattr(agents_base, "resolve", lambda *a, **k: model)


async def _lead_turn(monkeypatch: pytest.MonkeyPatch) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A running turn whose express step the Lead holds: (task, step, the asker)."""
    async with session_scope() as session:
        actor, _ = await member(session, "Asker")
        await session.flush()
        task = await task_ops.create_task(session, actor, goal="What's the time?", clarify="auto")
        task.transition(TaskStatus.PLANNING)
        await session.flush()
        view = await planning.draft_plan(session, task.id)
        step = await session.get(PlanStep, uuid.UUID(str(view.steps[0].id)))
        assert step is not None and step.spec["lane_role"] == "express"
        step.spec = {**step.spec, "agent": lead_rung.LEAD}
        await session.commit()
        ids = task.id, step.id, actor.id
    async with row_scope(Task, ids[0]) as session:
        await execution.advance(session, ids[0], TaskStatus.RUNNING)
        await session.commit()
    return ids


async def _planned_turn(monkeypatch: pytest.MonkeyPatch) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A Lead turn whose plan committed and whose first step is done: (task, Lead step, the asker)."""
    task_id, step_id, asker_id = await _lead_turn(monkeypatch)
    with monkeypatch.context() as m:

        async def run(agent: object, prompt: str, deps: object, **kwargs: object) -> object:
            return SimpleNamespace(output=_PLAN)

        m.setattr(agents_base, "build", scripted(run))
        assert await _step_run(task_id, step_id) == ""
    async with row_scope(Task, task_id) as session:
        (research,) = [s for s in await PlanStep.for_task(session, task_id) if s.title == "research"]
        research.state = StepState.DONE.value
        await session.commit()
    return task_id, step_id, asker_id


async def _step_run(task_id: uuid.UUID, step_id: uuid.UUID) -> str:
    async with row_scope(Task, task_id) as session:
        return await execution.execute_step(session, task_id, step_id)


async def _steer(actor_id: uuid.UUID, task_id: uuid.UUID, text: str = _STEER) -> Steer:
    async with row_scope(Task, task_id) as session:
        actor = await session.get(Actor, actor_id)
        assert actor is not None
        return await steering.steer(session, actor, task_id, text)


async def _steers(task_id: uuid.UUID) -> list[Steer]:
    async with row_scope(Task, task_id) as session:
        rows = await Steer.since(session, task_id, 0)
        for row in rows:
            session.expunge(row)
        return rows


async def _steps(task_id: uuid.UUID) -> list[PlanStep]:
    async with row_scope(Task, task_id) as session:
        rows = await PlanStep.for_task(session, task_id)
        for row in rows:
            session.expunge(row)
        return rows


async def _events[E](task_id: uuid.UUID, kind: type[E]) -> list[E]:
    async with row_scope(Task, task_id) as session:
        return [e for e in await TaskEvent.replay(session, task_id, since=0) if isinstance(e, kind)]


# ── a Lead run in flight ─────────────────────────────────────────────────────────────────────────


async def test_s1_a_call_the_lead_chose_before_a_steer_is_held_and_never_runs(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, step_id, asker_id = await _lead_turn(monkeypatch)
    requests: list[str] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(_told(messages))
        if len(requests) == 1:
            await _steer(asker_id, task_id, "answer for Paris")  # lands while the Lead chooses a call
            yield _call("web_fetch", {"url": "https://time.example/utc"}, "f1")
        else:
            yield "It is 15:02 in Paris."

    _model(monkeypatch, stream)
    assert await _step_run(task_id, step_id) == "It is 15:02 in Paris."

    assert len(requests) == 2 and "answer for Paris" not in requests[0]
    assert "**Asker steered** while you worked: answer for Paris" in requests[1]
    calls = [e for e in await _events(task_id, ToolCalled) if e.tool_call_id == "f1"]
    assert calls and all(not c.ok and c.denied_reason.startswith("held") for c in calls), "the call never ran"
    (row,) = await _steers(task_id)
    assert (row.state, row.safe_point, row.applied_step_id) == ("applied", "before_tool", step_id)


async def test_s2_a_steer_while_the_reply_streams_redirects_it_and_one_narration_stands(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, step_id, asker_id = await _lead_turn(monkeypatch)
    requests: list[str] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(_told(messages))
        if len(requests) == 1:
            await _steer(asker_id, task_id, "answer in French")  # lands while the reply is written
            yield "It is 14:02 UTC."
        else:
            yield "Il est 14 h 02 UTC."

    _model(monkeypatch, stream)
    assert await _step_run(task_id, step_id) == "Il est 14 h 02 UTC."

    assert "**Asker steered** before you finished: answer in French" in requests[1]
    narrations = [n.markdown for n in await _events(task_id, StepNarration) if n.step_id == step_id]
    assert narrations == ["Il est 14 h 02 UTC."], "the superseded partial never becomes the narration"
    (row,) = await _steers(task_id)
    assert (row.state, row.safe_point, row.applied_step_id) == ("applied", "final_answer", step_id)


async def test_s6_a_steer_while_the_lead_asks_reaches_the_run_the_answer_starts(
    monkeypatch: pytest.MonkeyPatch, recording_dispatcher: Any
) -> None:
    task_id, step_id, asker_id = await _lead_turn(monkeypatch)
    requests: list[str] = []
    ask = {
        "context": "Which city's time do you need?",
        "questions": [{"prompt": "Which city?", "type": "text"}],
    }

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(_told(messages))
        if len(requests) == 1:
            yield _call("ask_person", ask, "a1")
        else:
            yield "Il est 15 h 02 à Paris."

    _model(monkeypatch, stream)
    gate_id = (await _step_run(task_id, step_id)).removeprefix(execution.ASKING)
    await _steer(asker_id, task_id, "answer in French")  # the gate stays open
    async with row_scope(Task, task_id) as session:
        gate = await session.get(Gate, uuid.UUID(gate_id))
        actor = await session.get(Actor, asker_id)
        assert gate is not None and gate.resolved_at is None and actor is not None
        await gates.resolve(session, actor, gate.id, GateAction.ACCEPT, {"q1": "Paris"})
    (wake,) = [p for k, p, _ in recording_dispatcher.sends if k == f"step-{step_id}"]
    async with row_scope(Task, task_id) as session:
        await execution.store_answer(session, step_id, cast(dict[str, object], wake))
        await session.commit()
    assert await _step_run(task_id, step_id) == "Il est 15 h 02 à Paris."

    assert "answer in French" not in requests[0]
    assert (
        "Paris" in requests[1] and "**Asker steered**" in requests[-1] and "answer in French" in requests[-1]
    )
    (row,) = await _steers(task_id)
    assert (row.state, row.applied_step_id) == ("applied", step_id)
    assert row.safe_point in (SafePoint.BEFORE_MODEL.value, SafePoint.FINAL_ANSWER.value)


# ── a steer after the Lead planned: the replan seat at the frontier boundary ────────────────────


@dataclass
class _Seat:
    """What the replan seat's model was offered, per request: its tools and its words."""

    infos: list[AgentInfo] = field(default_factory=list[AgentInfo])
    told: list[str] = field(default_factory=list[str])


def _replan_seat(monkeypatch: pytest.MonkeyPatch, ending: str, args: dict[str, Any]) -> _Seat:
    """The replan seat ends in `ending` on every request."""
    seen = _Seat()

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        seen.infos.append(info)
        seen.told.append(_told(messages))
        yield _call(ending, args, "r1")

    _model(monkeypatch, stream)
    return seen


async def test_s4_a_revision_replaces_only_the_steps_not_started(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, lead_id, asker_id = await _planned_turn(monkeypatch)
    before = {s.title: s for s in await _steps(task_id)}
    row = await _steer(asker_id, task_id)
    seen = _replan_seat(monkeypatch, "revise_plan", _revision())

    assert await effects.steer_boundary(str(task_id)) is True

    (info,) = seen.infos
    assert {t.name for t in info.output_tools} == {"revise_plan", "keep_plan"}, "the seat's two endings only"
    assert info.function_tools == [] and not info.allow_text_output, "no tools and no free text"
    (prompt,) = seen.told
    assert _STEER in prompt and "research" in prompt and "write (writer)" in prompt
    after = await _steps(task_id)
    assert [s.id for s in after[:2]] == [lead_id, before["research"].id], "the Lead and the done step stand"
    assert before["write"].id not in {s.id for s in after}, "the step not started is replaced"
    (new,) = after[2:]
    assert (new.title, new.spec["agent"], new.epoch) == ("German memo", "writer", row.epoch)
    (settled,) = await _steers(task_id)
    assert (settled.state, settled.safe_point) == ("applied", "replan")
    (edit,) = [e for e in await _events(task_id, PlanEdited) if e.edit.kind is PlanEditKind.REPLAN]
    assert edit.edit.removed == [before["write"].id] and edit.edit.added == [new.id]
    applied = [a for a in await _events(task_id, SteerApplied) if a.command_id == row.command_id]
    assert [a.safe_point for a in applied] == [SafePoint.REPLAN]


async def test_s4_keep_plan_leaves_the_plan_untouched_and_is_not_asked_again(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _, asker_id = await _planned_turn(monkeypatch)
    before = await _steps(task_id)
    row = await _steer(asker_id, task_id)
    seen = _replan_seat(monkeypatch, "keep_plan", {"reason": "The research already covers only Germany."})

    assert await effects.steer_boundary(str(task_id)) is False, "a kept plan's frontier did not move"

    after = await _steps(task_id)
    assert [(s.id, s.title, s.spec, s.step_state) for s in after] == [
        (s.id, s.title, s.spec, s.step_state) for s in before
    ], "the plan is untouched"
    (write,) = [s for s in after if s.title == "write"]
    assert write.epoch == row.epoch, "the step not started carries the steer"
    (settled,) = await _steers(task_id)
    assert (settled.state, settled.safe_point) == ("applied", "replan")
    assert not [e for e in await _events(task_id, PlanEdited) if e.edit.kind is PlanEditKind.REPLAN]

    assert await effects.steer_boundary(str(task_id)) is False
    assert len(seen.infos) == 1, "the next boundary does not ask about the same steer again"


async def test_s4_a_revision_that_breaks_the_rules_keeps_the_plan(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _, asker_id = await _planned_turn(monkeypatch)
    before = [s.id for s in await _steps(task_id)]
    await _steer(asker_id, task_id)
    seen = _replan_seat(monkeypatch, "revise_plan", _revision(agent="chef"))

    assert await effects.steer_boundary(str(task_id)) is False

    assert len(seen.infos) == 3, "a broken revision goes back twice, then the plan stands"
    assert [s.id for s in await _steps(task_id)] == before
    (settled,) = await _steers(task_id)
    assert settled.state == "applied" and settled.safe_point == "replan"


def test_the_replan_seat_is_taken_only_on_a_replan() -> None:

    lead = get(lead_rung.LEAD)
    plain = definition_for(lead_rung.LEAD, TaskDeps(task_id=TaskId(uuid.uuid4())))
    seat = definition_for(lead_rung.LEAD, TaskDeps(task_id=TaskId(uuid.uuid4()), replanning=True))
    assert plain is lead and seat is lead.replan
    assert seat is not None and seat.tools == () and seat.effort == "medium"
