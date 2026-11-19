# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every step hears the user: the turn's own message reaches the brief of the step that works on it.

Found live (the Slack rows): a pasted thread rode only in the task's goal, the planner wrote each
step's goal as its own reading ("summarize the thread the person pasted"), and the step that had to
read the thread asked for it. Now every step carries the turn's message verbatim under its goal.
The Lead's plan and the scatter map are scripted; the plan written and the brief each step reads are what
is under test, for the five shapes a paste reaches a step through.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.context.brief import REQUEST_TITLE, prompt_for
from prismoid.db.models.actor import Actor
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import execution, planning
from prismoid.operations import swarm as swarm_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations.planning_turns import Routed
from prismoid.operations.turn import recipes
from prismoid.schemas.plan import (
    DeliverableKind,
    PlanProposal,
    ProposedStep,
    StepSpec,
    StepState,
    TaskStatus,
)
from prismoid.schemas.swarm import ScatterResult, SeatBrief
from tests.support.cast import member
from tests.support.lead_turn import lead_plans

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("silent_dispatcher")]

PASTE = (
    "[9:02] priya: the eu-west deploy is stuck on migration 0042, rolling back\n"
    "[9:05] omar: rollback done, error budget at 61%\n"
    "[9:11] priya: root cause is the lock on invoices_by_month, fix ships thursday"
)
ASK = f"Turn this Slack thread into a short incident memo for the team:\n\n{PASTE}"
FOLLOW_UP = f"Add these two lines from the follow-up thread too:\n\n{PASTE.upper()}"
READING = "Write the incident memo the person asked for"  # a planner's goal: its reading, no paste


def _plan_of(*agents: str) -> PlanProposal:
    """The Lead's plan: `agents` in order, each reading the one before, each goal a paraphrase."""
    steps = [
        ProposedStep(
            label=f"s{n}",
            agent=agent,
            title=f"{agent}'s part",
            goal=f"{READING} ({agent}'s part)",
            depends_on=[f"s{n - 1}"] if n > 1 else [],
            publishes=n == len(agents),
        )
        for n, agent in enumerate(agents, start=1)
    ]
    return PlanProposal(steps=steps, deliverable=DeliverableKind.FILES)


async def _briefs(session: AsyncSession, task_id: uuid.UUID, *, role: str | None = None) -> list[str]:
    """What each executable step of the task is told, in plan order (`role`: one lane part only)."""
    specs = [StepSpec.model_validate(r.spec) for r in await PlanStep.for_task(session, task_id)]
    return [prompt_for(s) for s in specs if s.contract is not None and (role is None or s.lane_role == role)]


async def _minted(session: AsyncSession, goal: str) -> Task:
    actor, _ = await member(session, "Paster")
    await session.flush()
    task = await task_ops.create_task(session, actor, goal=goal, clarify="never")
    task.transition(TaskStatus.PLANNING)
    await session.flush()
    return task


async def _planned(goal: str, *agents: str) -> tuple[uuid.UUID, uuid.UUID]:
    """A turn the Lead plans as `agents`: (the task, the Lead's step)."""
    async with session_scope() as session:
        task = await _minted(session, goal)
        await session.commit()
    return task.id, await lead_plans(task.id, _plan_of(*agents))


async def _work_briefs(task_id: uuid.UUID, lead_id: uuid.UUID) -> list[str]:
    """What each step the Lead planned is told, in plan order."""
    async with session_scope() as session:
        specs = [
            StepSpec.model_validate(r.spec)
            for r in await PlanStep.for_task(session, task_id)
            if r.id != lead_id
        ]
    return [prompt_for(s) for s in specs if s.contract is not None]


def _hears(brief: str, words: str) -> bool:
    return REQUEST_TITLE in brief and words in brief


async def test_a_writer_step_hears_the_paste() -> None:
    (brief,) = await _work_briefs(*await _planned(ASK, "writer"))
    assert brief.startswith(READING), "the goal leads; the person's words sit under it"
    assert _hears(brief, PASTE)


async def test_a_researcher_and_its_writer_both_hear_the_paste() -> None:
    briefs = await _work_briefs(*await _planned(ASK, "researcher", "writer"))
    assert len(briefs) == 2 and all(_hears(b, PASTE) for b in briefs)


async def test_turn_two_hears_its_own_message() -> None:
    """Turn 2's steps hear the follow-up's own words; turn 1's paste rides the mounted thread."""
    task_id, _ = await _planned(ASK, "writer")
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        assert task is not None
        for row in await PlanStep.for_task(session, task_id):
            row.state = StepState.DONE.value
        await session.flush()
        await execution.deliver(session, task_id, "Done.")
        actor = await session.get(Actor, task.creator_actor_id)
        assert actor is not None
        await task_ops.continue_task(session, actor, task_id, goal=FOLLOW_UP, clarify="never")
        await session.commit()
        done = {r.id for r in await PlanStep.for_task(session, task_id)}
    lead_id = await lead_plans(task_id, _plan_of("writer"))
    async with session_scope() as session:
        rows = await PlanStep.for_task(session, task_id)
        (spec,) = [StepSpec.model_validate(r.spec) for r in rows if r.id not in done and r.id != lead_id]
    brief = prompt_for(spec)
    assert _hears(brief, PASTE.upper())
    assert ASK not in brief, "turn 1's ask is the thread's, never this turn's request"


async def test_the_leads_step_hears_the_paste_once_and_its_plan_under_each_goal() -> None:
    """The Lead's goal IS the message, so its brief says it once; the steps it plans carry it under their own goals."""
    goal = f"thoughts on this?\n\n{PASTE}"
    task_id, lead_id = await _planned(goal, "writer")
    async with session_scope() as session:
        lead = await session.get(PlanStep, lead_id)
        assert lead is not None
        lead_brief = prompt_for(StepSpec.model_validate(lead.spec))
    assert lead_brief.count(PASTE) == 1 and REQUEST_TITLE not in lead_brief
    (brief,) = await _work_briefs(task_id, lead_id)
    assert _hears(brief, PASTE)


async def test_every_scatter_seat_hears_the_ask() -> None:
    """A seat's goal is the map's slice; the person's ask rides under it."""
    goal = "Find at least 8 open-source vector databases; for each give the license and the repo URL."
    async with session_scope() as session:
        task = await _minted(session, goal)
        lane = recipes.lane_of(swarm_ops.scatter_plan(goal), "scatter", "detector", "a stored sweep")
        await planning.commit_plan(
            session, task, Routed.of(lane, None), base_ordinal=0, drafted=task.plan_epoch
        )
        await session.commit()
        (map_row,) = await PlanStep.for_task(session, task.id)
        scatter = ScatterResult(
            objective="vector databases with license and repo",
            entity="an open-source vector database",
            target_count=8,
            record_fields=["license", "repo"],
            seats=[
                SeatBrief(
                    id=f"g0{i}",
                    goal=f"Sweep source {i}",
                    not_here=f"every source but {i}",
                    expected="a database",
                )
                for i in (1, 2)
            ],
        )
        await swarm_ops.expand_after_scatter(session, task, map_row, scatter)
        await session.commit()
        seats = await _briefs(session, task.id, role="gather")
    assert len(seats) == 2 and all(_hears(b, goal) for b in seats)
