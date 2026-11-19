# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Lead's endings, committed (ADR-092): the real `execute_step` on the Lead's step, which `draft_plan`
seats on every turn no recipe answers, its run scripted. Every ending lands in the step's one commit
and is on the record with how it ended, who decided and why; a crash before that commit leaves nothing;
the Lead cut at its wall says it ran out of time and plans nothing; a plan the commit refuses keeps its
ending and says why; an ask parks and its answer starts a fresh Lead run, its answers the task's; the
person sees "Thinking" before anything else."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import select

import prismoid.ai.agents.base as agents_base
from prismoid.ai.capabilities import lead_check
from prismoid.ai.deps import TaskDeps
from prismoid.conf import settings
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.gate import Gate
from prismoid.db.models.inbox import InboxItem, InboxKind
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.routine import Routine
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.errors import StepFailed
from prismoid.operations import execution, gates, live, planning
from prismoid.operations import tasks as task_ops
from prismoid.operations.execution import lead_rung
from prismoid.operations.planning_steps import turn_ask
from prismoid.operations.turn import commit as turn_commit
from prismoid.operations.turn import intake as turn_intake
from prismoid.schemas import sizing
from prismoid.schemas.events import (
    Activity,
    EscalationRaised,
    PlanEdited,
    StepStateChanged,
    TaskPlanned,
    TaskProposed,
)
from prismoid.schemas.inputs import InputSet
from prismoid.schemas.lead import AskUser, Fail, FailKind, LeadEndingKind, ProposeTask, SubmitPlan
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import (
    DeliverableKind,
    GateAction,
    GateKind,
    PlanEditKind,
    PlanProposal,
    ProposedQuestion,
    ProposedStep,
    QuestionType,
    StepState,
    TaskStatus,
)
from prismoid.schemas.routines import RoutineStatus
from prismoid.schemas.sizing import SizeBucket
from tests.support.cast import member
from tests.support.dispatchers import RecordingDispatcher
from tests.support.runners import scripted

pytestmark = pytest.mark.integration

_PLAN = SubmitPlan(
    say="I'll write the memo now.",
    plan=PlanProposal(
        steps=[
            ProposedStep(
                label="s1",
                agent="writer",
                title="Memo",
                goal="Write the memo",
                publishes=True,
            )
        ],
        deliverable=DeliverableKind.FILES,
    ),
)


async def _lead_turn(monkeypatch: pytest.MonkeyPatch) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A turn whose express step the Lead holds, running: (task, step, the asker)."""

    async with session_scope() as session:
        actor, _ = await member(session, "Asker")
        await session.flush()
        task = await task_ops.create_task(session, actor, goal="What's the time?", clarify="auto")
        task.transition(TaskStatus.PLANNING)
        await session.flush()
        view = await planning.draft_plan(session, task.id)
        step = await session.get(PlanStep, uuid.UUID(str(view.steps[0].id)))
        assert step is not None and step.spec["lane_role"] == "express"
        assert step.spec["agent"] == lead_rung.LEAD, "every turn no recipe answers is the Lead's"
        await session.commit()
        ids = task.id, step.id, actor.id
    async with row_scope(Task, ids[0]) as session:
        await execution.advance(session, ids[0], TaskStatus.RUNNING)
        await session.commit()
    return ids


def _lead_says(monkeypatch: pytest.MonkeyPatch, *outputs: object) -> list[TaskDeps]:
    """The Lead's runs end in `outputs`, one per run; the deps each run was built with."""
    seen: list[TaskDeps] = []

    async def run(agent: object, prompt: str, deps: TaskDeps, **kwargs: object) -> object:
        seen.append(deps)
        return SimpleNamespace(output=outputs[len(seen) - 1])

    monkeypatch.setattr(agents_base, "build", scripted(run))
    return seen


async def _step_run(task_id: uuid.UUID, step_id: uuid.UUID) -> str:
    async with row_scope(Task, task_id) as session:
        return await execution.execute_step(session, task_id, step_id)


async def _events[E](task_id: uuid.UUID, kind: type[E], types: Sequence[str]) -> list[E]:
    async with row_scope(Task, task_id) as session:
        return [e for e in await TaskEvent.of_types(session, task_id, tuple(types)) if isinstance(e, kind)]


async def _ending(task_id: uuid.UUID, step_id: uuid.UUID) -> LeadEndingKind | None:
    frames = await _events(task_id, StepStateChanged, ["step.state"])
    (done,) = [f for f in frames if f.step_id == step_id and f.state is StepState.DONE]
    return done.ending


async def test_a_reply_is_the_express_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, step_id, _ = await _lead_turn(monkeypatch)
    _lead_says(monkeypatch, "It is 14:02 UTC.")
    assert await _step_run(task_id, step_id) == "It is 14:02 UTC."
    assert await _ending(task_id, step_id) is LeadEndingKind.REPLY
    async with row_scope(Task, task_id) as session:
        await execution.deliver(session, task_id, "It is 14:02 UTC.")
        await session.commit()
        task = await session.get(Task, task_id)
        assert task is not None and task.state is TaskStatus.DELIVERED
        assert len(await PlanStep.for_task(session, task_id)) == 1


async def test_a_routine_asked_for_in_chat_is_saved_born_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Lead holds `routine_create`; its run is handed the hands that save the routine paused and knock."""
    task_id, step_id, asker = await _lead_turn(monkeypatch)
    said: list[str] = []

    async def run(agent: object, prompt: str, deps: TaskDeps, **kwargs: object) -> object:
        assert deps.propose_routine is not None, "the Lead's routine tool has no hands"
        said.append(
            await deps.propose_routine(
                {"name": "Monday digest", "instruction": "Send me the news", "every": "7d"}
            )
        )
        return SimpleNamespace(output="Saved it, paused until you resume it.")

    monkeypatch.setattr(agents_base, "build", scripted(run))
    await _step_run(task_id, step_id)
    assert said and "PAUSED" in said[0]
    async with session_scope() as session:
        (routine,) = (await session.scalars(select(Routine).where(Routine.owner_actor_id == asker))).all()
        assert routine.name == "Monday digest" and routine.status == RoutineStatus.PAUSED
        knocks = await session.scalars(
            select(InboxItem).where(InboxItem.actor_id == asker, InboxItem.kind == InboxKind.ROUTINE_PAUSED)
        )
        assert len(knocks.all()) == 1


async def test_a_plan_joins_the_turn_on_the_record(monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, step_id, _ = await _lead_turn(monkeypatch)
    _lead_says(monkeypatch, _PLAN)
    assert await _step_run(task_id, step_id) == "", "the plan's steps deliver the turn, not the Lead's line"
    assert await _ending(task_id, step_id) is LeadEndingKind.PLAN
    (edit,) = await _events(task_id, PlanEdited, ["plan.edited"])
    assert edit.edit.kind is PlanEditKind.LEAD and edit.edit.reason == _PLAN.say
    planned = await _events(task_id, TaskPlanned, ["task.planned"])
    assert [p.route_source for p in planned] == ["lead", "lead"]
    assert planned[-1].route == "standard" and planned[-1].route_why == _PLAN.say
    async with row_scope(Task, task_id) as session:
        (memo,) = [s for s in await PlanStep.for_task(session, task_id) if s.id != step_id]
        assert memo.spec["agent"] == "writer" and memo.step_state is StepState.READY


async def test_a_proposal_answers_with_the_card(monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, step_id, _ = await _lead_turn(monkeypatch)
    offer = ProposeTask(
        answer="It is 14:02 UTC.", goal="A world clock sheet", deliverable=ArtifactType.SPREADSHEET
    )
    _lead_says(monkeypatch, offer)
    assert await _step_run(task_id, step_id) == offer.answer
    assert await _ending(task_id, step_id) is LeadEndingKind.PROPOSE
    (card,) = await _events(task_id, TaskProposed, ["task.proposed"])
    assert card.goal == offer.goal and card.deliverable == "spreadsheet" and card.step_id == step_id


async def test_a_proposed_documents_format_rides_the_card(monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, step_id, _ = await _lead_turn(monkeypatch)
    offer = ProposeTask(answer="Here.", goal="The summary", deliverable=ArtifactType.DOCUMENT, format="docx")
    _lead_says(monkeypatch, offer)
    await _step_run(task_id, step_id)
    (card,) = await _events(task_id, TaskProposed, ["task.proposed"])
    assert (card.deliverable, card.deliverable_format) == ("document", "docx")


async def test_an_unattended_failure_fails_the_step_and_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, step_id, _ = await _lead_turn(monkeypatch)
    _lead_says(monkeypatch, Fail(kind=FailKind.NEEDS_OWNER, reason="the mailbox is not connected"))
    with pytest.raises(StepFailed, match="needs_owner"):
        await _step_run(task_id, step_id)
    frames = await _events(task_id, StepStateChanged, ["step.state"])
    (stop,) = [f for f in frames if f.step_id == step_id and f.state is StepState.ESCALATED]
    assert stop.ending is LeadEndingKind.FAIL
    (raised,) = await _events(task_id, EscalationRaised, ["escalation.raised"])
    assert raised.diagnosis.why == "the mailbox is not connected"


async def test_an_ask_parks_and_its_answer_starts_a_fresh_lead_run(
    monkeypatch: pytest.MonkeyPatch, recording_dispatcher: RecordingDispatcher
) -> None:
    task_id, step_id, asker_id = await _lead_turn(monkeypatch)
    ask = AskUser(
        context="Which city's time do you need?",
        questions=[ProposedQuestion(prompt="Which city?", type=QuestionType.TEXT)],
    )
    seen = _lead_says(monkeypatch, ask, "It is 15:02 in Paris.")
    gate_id = (await _step_run(task_id, step_id)).removeprefix(execution.ASKING)
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        gate = await session.get(Gate, uuid.UUID(gate_id))
        assert task is not None and task.state is TaskStatus.WAITING
        assert gate is not None and gate.kind == GateKind.INPUT.value and gate.step_id == step_id
        actor = await session.get(Actor, asker_id)
        assert actor is not None
        await gates.resolve(session, actor, gate.id, GateAction.ACCEPT, {"q1": "Paris"})
    (wake,) = [p for k, p, _ in recording_dispatcher.sends if k == f"step-{step_id}"]
    async with row_scope(Task, task_id) as session:
        await execution.store_answer(session, step_id, cast(dict[str, object], wake))
        await session.commit()
    assert await _step_run(task_id, step_id) == "It is 15:02 in Paris."
    assert [d.lead_facts.asked for d in seen if d.lead_facts] == [False, True], (
        "the answer's run may not ask again"
    )
    assert await _ending(task_id, step_id) is LeadEndingKind.REPLY
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        assert task is not None
        # What `/task/answers.json` mounts for every step a plan seats, and what a step's same question is
        # answered from: each answer carrying its question.
        assert task.answers == {"q1": {"question": "Which city?", "answer": "Paris"}}


async def test_a_crash_before_the_commit_leaves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, step_id, _ = await _lead_turn(monkeypatch)
    _lead_says(monkeypatch, _PLAN)

    async def crash(*args: object, **kwargs: object) -> object:
        raise RuntimeError("the worker died mid-commit")

    monkeypatch.setattr(planning, "commit_plan", crash)
    with pytest.raises(RuntimeError, match="mid-commit"):
        await _step_run(task_id, step_id)
    assert await _events(task_id, PlanEdited, ["plan.edited"]) == []
    assert [p.route_source for p in await _events(task_id, TaskPlanned, ["task.planned"])] == ["lead"]
    async with row_scope(Task, task_id) as session:
        (step,) = await PlanStep.for_task(session, task_id)
        assert step.step_state is not StepState.DONE


async def test_the_lead_past_its_wall_says_it_ran_out_of_time_and_plans_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, step_id, _ = await _lead_turn(monkeypatch)
    monkeypatch.setattr(settings.worker, "express_timeout_s", 0)
    monkeypatch.setattr(settings.worker, "chat_timeout_s", 0)

    async def slow(agent: object, prompt: str, deps: TaskDeps, **kwargs: object) -> object:
        await asyncio.sleep(5)
        raise AssertionError("the wall stops the run first")

    monkeypatch.setattr(agents_base, "build", scripted(slow))
    assert await _step_run(task_id, step_id) == lead_rung.WALL_EMPTY
    assert await _ending(task_id, step_id) is LeadEndingKind.REPLY
    assert await _events(task_id, PlanEdited, ["plan.edited"]) == [], "never a document nobody asked for"
    async with row_scope(Task, task_id) as session:
        assert len(await PlanStep.for_task(session, task_id)) == 1


async def test_the_lead_decides_by_its_wall_and_writes_on_to_a_replys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, step_id, _ = await _lead_turn(monkeypatch)
    monkeypatch.setattr(settings.worker, "express_timeout_s", 0)
    seen: list[float | None] = []

    async def writes(agent: object, prompt: str, deps: TaskDeps, **kwargs: object) -> object:
        seen.append(deps.deadline)
        await asyncio.sleep(0.2)  # past the deciding wall, inside a reply's
        return SimpleNamespace(output="Pinecone is hosted.")

    monkeypatch.setattr(agents_base, "build", scripted(writes))
    assert await _step_run(task_id, step_id) == "Pinecone is hosted."
    (deadline,) = seen
    assert deadline is not None and deadline <= time.monotonic(), "its web tools stop at the deciding wall"


async def test_reading_the_turns_attachments_never_eats_the_leads_wall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, step_id, _ = await _lead_turn(monkeypatch)
    monkeypatch.setattr(settings.worker, "express_timeout_s", 1)
    monkeypatch.setattr(settings.worker, "chat_timeout_s", 1)
    real = lead_rung.brief

    async def slow_reads(*args: Any, **kwargs: Any) -> lead_rung.LeadTurn:
        await asyncio.sleep(1.5)  # a sidecar conversion longer than the whole wall
        return await real(*args, **kwargs)

    started: list[tuple[float, float | None]] = []

    async def decides(agent: object, prompt: str, deps: TaskDeps, **kwargs: object) -> object:
        started.append((time.monotonic(), deps.deadline))
        return SimpleNamespace(output="It is 14:02 UTC.")

    monkeypatch.setattr(lead_rung, "brief", slow_reads)
    monkeypatch.setattr(agents_base, "build", scripted(decides))
    assert await _step_run(task_id, step_id) == "It is 14:02 UTC.", "the wall starts when the Lead does"
    ((at, deadline),) = started
    assert deadline is not None and deadline - at > 0.5


async def test_a_plan_the_commit_refuses_keeps_its_ending_and_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, step_id, _ = await _lead_turn(monkeypatch)
    act = SubmitPlan(
        say="I'll have the operator book it on uber.com.",
        plan=PlanProposal(
            steps=[
                ProposedStep(
                    label="s1",
                    agent="operator",
                    title="Book the cab",
                    goal="Book it",
                    hosts=["uber.com"],
                    depends_on=["s9"],  # a step that does not exist: the commit's graph rules refuse it
                )
            ],
            deliverable=DeliverableKind.ACTION,
        ),
    )
    _lead_says(monkeypatch, act)
    assert await _step_run(task_id, step_id) == ""
    (edit,) = await _events(task_id, PlanEdited, ["plan.edited"])
    assert edit.edit.reason == lead_check.FLOOR_SAY, "the Lead's stale line never stands over another plan"
    planned = (await _events(task_id, TaskPlanned, ["task.planned"]))[-1]
    assert planned.fallback == lead_check.REFUSED_WHY and "s9" in planned.route_why
    async with row_scope(Task, task_id) as session:
        (only,) = [s for s in await PlanStep.for_task(session, task_id) if s.id != step_id]
        assert only.spec["agent"] == "operator", "an action stays an action, never a writer's document"


async def test_a_dialed_size_bands_the_leads_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    async with session_scope() as session:
        actor, _ = await member(session, "Sizer")
        await session.flush()
        task = await task_ops.create_task(session, actor, goal="Ten cafes in Lisbon", scale="brief")
        await session.flush()
        sheet = PlanProposal(
            steps=[
                ProposedStep(
                    label="s1",
                    agent="maker",
                    title="Cafes",
                    goal="List them",
                    publishes=True,
                    produces=ArtifactType.COLLECTION,
                )
            ],
            deliverable=DeliverableKind.FILES,
        )
        ask = await turn_ask(session, task)
        [(_, spec)], _, _ = await turn_commit.accepted_lead_plan(session, task, sheet, ask, InputSet())
        size = turn_intake.turn_size(await turn_intake.read_rows(session, task))
        assert size is not None and size[0] is SizeBucket.BRIEF
        assert spec.contract is not None
        for line in sizing.size_clause(ArtifactType.COLLECTION, *size):
            assert line in spec.contract.acceptance, "the person's size reaches the step's acceptance"
        await session.rollback()


async def test_thinking_is_the_first_thing_the_person_sees(monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, step_id, _ = await _lead_turn(monkeypatch)
    sent: list[Any] = []
    real = live.notify

    async def recording(event: Any) -> None:
        sent.append(event)
        await real(event)

    monkeypatch.setattr(live, "notify", recording)
    _lead_says(monkeypatch, "It is 14:02 UTC.")
    await _step_run(task_id, step_id)
    first = sent[0]
    assert isinstance(first, Activity) and first.kind == "phase" and first.label == lead_rung.THINKING
    assert first.tool == lead_rung.LEAD
