# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Talk until it is work (ADR-058), as the Lead keeps it (ADR-092), pinned end to end minus the queues.

Every new turn no dial's recipe answers is the Lead's one frameless step, decided by no model before it:
a reaction on a follow-up, a turn nobody can prove talk or work, and named work alike.
The binding law: a carried plan stamp is never a dial on a later turn; a USER dial still binds the plan
the Lead makes.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import pytest

import prismoid.ai.agents.base as agents_base
from prismoid.ai.deps import TaskDeps
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import execution, planning
from prismoid.operations import tasks as task_ops
from prismoid.operations.turn import intake as turn_intake
from prismoid.schemas.events import TaskPlanned
from prismoid.schemas.lead import LeadEndingKind
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import (
    DeliverableKind,
    PlanProposal,
    PlanView,
    ProposedStep,
    StepSpec,
    StepState,
    TaskStatus,
)
from tests.support.cast import member
from tests.support.lead_turn import lead_plans
from tests.support.runners import scripted

pytestmark = pytest.mark.integration

_WRITES = PlanProposal(
    steps=[ProposedStep(label="s1", agent="writer", title="Memo", goal="write it", publishes=True)],
    deliverable=DeliverableKind.FILES,
)
_RESEARCHES = PlanProposal(
    steps=[ProposedStep(label="s1", agent="researcher", title="Read", goal="read up")],
    deliverable=DeliverableKind.ANSWER,
)


async def _new_task(goal: str, **dials: object) -> uuid.UUID:
    async with session_scope() as session:
        actor, _ = await member(session, "Talker")
        await session.flush()
        task = await task_ops.create_task(session, actor, goal=goal, clarify="never", **dials)  # pyright: ignore[reportArgumentType]
        task.transition(TaskStatus.PLANNING)
        await session.flush()
        await session.commit()
        return task.id


async def _plan_turn(task_id: uuid.UUID):
    async with row_scope(Task, task_id) as session:
        view = await planning.draft_plan(session, task_id)
        await session.commit()
    async with row_scope(Task, task_id) as session:
        planned = await TaskEvent.latest_of_type(session, task_id, "task.planned")
    assert isinstance(planned, TaskPlanned)
    return view, planned


def _is_the_leads(view: PlanView, planned: TaskPlanned) -> None:
    steps = view.steps
    assert planned.route == "express" and planned.route_source == "lead" and planned.route_why
    assert steps[-1].agent == "lead" and steps[-1].lane_role == "express"
    assert steps[-1].acceptance == [], "the Lead's brief is the ask itself"


Canned = Callable[..., Awaitable[object]]


async def _run_express_step(
    task_id: uuid.UUID, step_id: object, monkeypatch: pytest.MonkeyPatch, canned: Canned
) -> str:
    monkeypatch.setattr(agents_base, "build", scripted(canned))

    import prismoid.ai.frames as frames

    async def exploding_frame(*args: object, **kwargs: object) -> object:
        raise AssertionError("an express step must never create a frame (ADR-049 §3)")

    monkeypatch.setattr(frames, "new_frame", exploding_frame)
    async with row_scope(Task, task_id) as session:
        await execution.advance(session, task_id, TaskStatus.RUNNING)
        await session.commit()
    async with row_scope(Task, task_id) as session:
        return await execution.execute_step(session, task_id, uuid.UUID(str(step_id)))


async def _deliver(task_id: uuid.UUID, summary: str) -> None:
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        assert task is not None
        if task.state is TaskStatus.PLANNING:
            await execution.advance(session, task_id, TaskStatus.RUNNING)
        await execution.deliver(session, task_id, summary)
        await session.commit()


async def _follow_up(task_id: uuid.UUID, goal: str, **dials: object) -> None:
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        assert task is not None
        actor = await session.get(Actor, task.creator_actor_id)
        assert actor is not None
        await task_ops.continue_task(session, actor, task_id, goal=goal, **dials)  # pyright: ignore[reportArgumentType]
        await session.commit()


# ── the thread, replayed ──────────────────────────────────────────────────────────────────


async def test_close_on_a_follow_up_is_the_leads_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = await _new_task("who is karl schwarzschild")
    view, planned = await _plan_turn(task_id)
    _is_the_leads(view, planned)

    async def canned(agent: object, prompt: str, deps: TaskDeps, **kw: object) -> object:
        return SimpleNamespace(output="A German physicist who found the first exact solution.")

    await _run_express_step(task_id, view.steps[0].id, monkeypatch, canned)
    await _deliver(task_id, "A German physicist who found the first exact solution.")

    await _follow_up(task_id, "close")
    view2, planned2 = await _plan_turn(task_id)
    _is_the_leads(view2, planned2)
    assert view2.steps[-1].id != view.steps[0].id, "the follow-up is a new Lead step"


async def test_a_turn_nobody_can_prove_talk_or_work_is_the_leads(monkeypatch: pytest.MonkeyPatch) -> None:
    task_id = await _new_task("Karl Schwarzschild")
    view, planned = await _plan_turn(task_id)
    _is_the_leads(view, planned)


async def test_named_work_is_the_leads_turn_too(monkeypatch: pytest.MonkeyPatch) -> None:
    task_id = await _new_task("Write me a memo on the Eddington correction")
    view, planned = await _plan_turn(task_id)
    _is_the_leads(view, planned)


# ── the binding law ───────────────────────────────────────────────────────────────────────


async def test_a_plan_sourced_stamp_never_binds_a_later_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    task_id = await _new_task("Analyze the history of the Schwarzschild solution and tell me what mattered")
    await lead_plans(task_id, _WRITES)
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        assert task is not None
        assert task.deliverable == "document" and task.deliverable_source == "plan"
        for s in await PlanStep.for_task(session, task_id):
            s.state = StepState.DONE.value
        await session.commit()
    await _deliver(task_id, "done")

    await _follow_up(task_id, "Analyze the Eddington angle next")
    view, planned = await _plan_turn(task_id)
    _is_the_leads(view, planned)
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        assert task is not None
        facts = (await turn_intake.intake(session, task)).facts
    assert facts.deliverable is None, "a carried plan stamp is read-back, never a constraint"
    assert LeadEndingKind.REPLY in facts.allowed_endings()


async def test_a_user_dial_still_binds_the_leads_plan_on_a_later_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = await _new_task("Analyze our churn", deliverable="document")
    _, planned = await _plan_turn(task_id)
    assert planned.route_source == "dial", "the dial IS the plan"
    async with row_scope(Task, task_id) as session:
        for s in await PlanStep.for_task(session, task_id):
            s.state = StepState.DONE.value
        await session.commit()
    await _deliver(task_id, "done")
    await _follow_up(task_id, "Analyze the retention side too")
    await lead_plans(task_id, _RESEARCHES)
    async with row_scope(Task, task_id) as session:
        planned2 = await TaskEvent.latest_of_type(session, task_id, "task.planned")
        rows = await PlanStep.for_task(session, task_id)
        last = StepSpec.model_validate(rows[-1].spec)
    assert isinstance(planned2, TaskPlanned) and planned2.route_source == "lead"
    assert planned2.fallback, "a plan ending in no document breaks the carried user dial"
    assert last.contract is not None and last.contract.output is not None
    assert last.contract.output.type is ArtifactType.DOCUMENT, "the user's dial still binds the delivery"
