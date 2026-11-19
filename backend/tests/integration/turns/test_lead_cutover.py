# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The cutover (ADR-092): every new turn no recipe answers is the Lead's, a first turn, a follow-up, a fork and a
routine fire alike, and the person sees the Lead thinking within the first-delta budget; a dial's recipe wears
`route_source=dial` and seats no Lead."""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import prismoid.ai.agents.base as agents_base
from prismoid.ai.deps import TaskDeps
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import execution, live, planning
from prismoid.operations import routines as routine_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations.execution import lead_rung
from prismoid.operations.turn import route
from prismoid.schemas.events import Activity, TaskPlanned
from prismoid.schemas.lead import Deliver, LeadEndingKind, TurnOrigin
from prismoid.schemas.plan import TaskStatus
from tests.support.cast import member
from tests.support.runners import scripted

pytestmark = pytest.mark.integration

FIRST_DELTA_S = 2.0


def _lead_says(monkeypatch: pytest.MonkeyPatch, output: object) -> list[TaskDeps]:
    """Every Lead run ends in `output`, with no model; the deps each run was built with."""
    seen: list[TaskDeps] = []

    async def run(agent: object, prompt: str, deps: TaskDeps, **kwargs: object) -> object:
        seen.append(deps)
        return SimpleNamespace(output=output)

    monkeypatch.setattr(agents_base, "build", scripted(run))
    return seen


async def _routed(task_id: uuid.UUID) -> tuple[TaskPlanned, list[tuple[uuid.UUID, dict[str, Any]]]]:
    """Draft the PLANNING task's turn; its routed record and its steps' (id, spec)."""
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        assert task is not None
        lead_decides = await route.lead_decides(session, task)
        await planning.draft_plan(session, task_id)
        await session.commit()
        planned = await TaskEvent.latest_of_type(session, task_id, "task.planned")
        assert isinstance(planned, TaskPlanned)
        steps = [
            (s.id, dict(s.spec))
            for s in await PlanStep.for_task(session, task_id)
            if s.turn_epoch == task.turn_epoch
        ]
    assert lead_decides is (planned.route_source == "lead"), "run_task's read agrees with the route"
    return planned, steps


def _is_the_leads(planned: TaskPlanned, steps: list[tuple[uuid.UUID, dict[str, Any]]]) -> uuid.UUID:
    """The turn's one step is the Lead's, on the express rung; its id."""
    assert (planned.route, planned.route_source) == ("express", "lead")
    ((lead, spec),) = steps
    assert spec["agent"] == lead_rung.LEAD and spec["lane_role"] == "express"
    return lead


async def _run(task_id: uuid.UUID, step_id: uuid.UUID) -> str:
    async with row_scope(Task, task_id) as session:
        await execution.advance(session, task_id, TaskStatus.RUNNING)
        await session.commit()
    async with row_scope(Task, task_id) as session:
        return await execution.execute_step(session, task_id, step_id)


async def _submitted(goal: str, **dials: Any) -> tuple[Actor, uuid.UUID]:
    """A person's task with `dials`, PLANNING its first turn: (the person, the task)."""
    async with session_scope() as session:
        actor, _ = await member(session, "Asker")
        await session.flush()
        task = await task_ops.create_task(session, actor, goal=goal, clarify="auto", **dials)
        task.transition(TaskStatus.PLANNING)
        await session.commit()
        return actor, task.id


async def test_a_first_turn_is_one_lead_step(monkeypatch: pytest.MonkeyPatch) -> None:
    _, task_id = await _submitted("Write me a memo on the Eddington correction")
    _is_the_leads(*await _routed(task_id))


async def test_a_follow_up_turn_is_the_leads(monkeypatch: pytest.MonkeyPatch) -> None:
    actor, task_id = await _submitted("What's the time?")
    lead = _is_the_leads(*await _routed(task_id))
    _lead_says(monkeypatch, "It is 14:02 UTC.")
    await _run(task_id, lead)
    async with row_scope(Task, task_id) as session:
        await execution.deliver(session, task_id, "It is 14:02 UTC.")
        await session.commit()
    async with row_scope(Task, task_id) as session:
        await task_ops.continue_task(session, actor, task_id, goal="and in Paris?")
        await session.commit()
    second = _is_the_leads(*await _routed(task_id))
    assert second != lead


async def test_a_fork_is_the_leads(monkeypatch: pytest.MonkeyPatch) -> None:
    actor, parent_id = await _submitted("What's the time?")
    async with row_scope(Task, parent_id) as session:
        child = await task_ops.fork(session, actor, parent_id)
        child.transition(TaskStatus.PLANNING)
        await session.commit()
        child_id = child.id
    _is_the_leads(*await _routed(child_id))


async def test_a_routine_turn_is_the_leads_with_the_unattended_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    actor, task_id = await _submitted("Summarise what changed today")
    async with row_scope(Task, task_id) as session:
        schedule = {"cron": "0 9 * * 1-5", "every_s": None, "at": None, "timezone": "UTC"}
        made = await routine_ops.create_routine(
            session, actor, name=f"digest-{uuid.uuid4().hex[:8]}", instruction="the digest", schedule=schedule
        )
        task = await session.get(Task, task_id)
        assert task is not None
        task.origin_routine_id = made.id
        await session.commit()
    lead = _is_the_leads(*await _routed(task_id))
    seen = _lead_says(monkeypatch, Deliver(result="Nothing changed today.", findings=False))
    await _run(task_id, lead)
    (deps,) = seen
    facts = deps.lead_facts
    assert facts is not None and facts.unattended and facts.origin is TurnOrigin.ROUTINE
    assert LeadEndingKind.REPLY not in facts.allowed_endings()
    assert LeadEndingKind.ASK not in facts.allowed_endings()


async def test_a_dialed_turn_is_its_recipe_and_seats_no_lead() -> None:
    _, task_id = await _submitted("Compare three CRMs", deliverable="spreadsheet")
    planned, steps = await _routed(task_id)
    assert planned.route_source == "dial"
    assert all(spec["agent"] != lead_rung.LEAD for _, spec in steps)


async def test_the_lead_is_thinking_within_the_first_delta_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    _, task_id = await _submitted("What's the time?")
    sent: list[tuple[float, Any]] = []
    real = live.notify

    async def timed(event: Any) -> None:
        sent.append((time.monotonic(), event))
        await real(event)

    monkeypatch.setattr(live, "notify", timed)
    _lead_says(monkeypatch, "It is 14:02 UTC.")
    began = time.monotonic()
    lead = _is_the_leads(*await _routed(task_id))
    await _run(task_id, lead)
    thinking = [at for at, e in sent if isinstance(e, Activity) and e.label == lead_rung.THINKING]
    assert thinking and thinking[0] - began < FIRST_DELTA_S, (
        f"the Lead's face came {thinking[0] - began:.2f} s in"
    )
