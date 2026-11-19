# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Deliverable selection (ADR-015): the door validates it, the plan honors it, and the
kind's skill rides along - plus the skills a Lead's plan names, pinned per step, with the Lead scripted.

Needs the stack (`just up`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.errors import ValidationFailed
from prismoid.operations import planning
from prismoid.operations import tasks as task_ops
from prismoid.schemas.plan import DeliverableKind, PlanProposal, ProposedStep, StepSpec, TaskStatus
from tests.support.cast import member
from tests.support.lead_turn import lead_plans

pytestmark = pytest.mark.integration


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


@pytest.fixture
async def actor(db: AsyncSession) -> Actor:
    row, _ = await member(db, "Ana")
    await db.commit()
    return row


async def test_the_door_refuses_a_kind_no_agent_publishes(db: AsyncSession, actor: Actor) -> None:
    with pytest.raises(ValidationFailed):
        await task_ops.create_task(db, actor, goal="g", deliverable="webapp")
    with pytest.raises(ValidationFailed):
        await task_ops.create_task(db, actor, goal="g", deliverable="board")  # the alias is CLI-side


async def test_a_chosen_deliverable_pins_its_kind_skill_on_one_typed_step(
    db: AsyncSession, actor: Actor
) -> None:
    """A chosen deliverable is its recipe, no model asked: the maker's one step, its kind's format skill
    pinned on the task deterministically - the dropdown's promise."""
    task = await task_ops.create_task(db, actor, goal="track the vendors", deliverable="collection")
    from prismoid.operations import execution

    await execution.advance(db, task.id, TaskStatus.PLANNING)
    view = await planning.draft_plan(db, task.id)
    await db.commit()

    [step] = view.steps
    assert step.agent == "maker"
    await db.refresh(task)
    assert [p["name"] for p in task.skill_pins] == ["organize"]


async def test_the_skill_a_leads_step_names_is_pinned_on_that_step(db: AsyncSession, actor: Actor) -> None:
    """Auto (no deliverable, no pins): a skill the Lead's plan names on a step is pinned on that step at
    its published head, and the task row pins nothing; whether the Lead picks well is the routing suite's."""
    task = await _planning(db, actor, "turn these vendors into something trackable")
    await lead_plans(task.id, _one("maker", "Track the vendors", skills=["organize"], produces="collection"))

    await db.refresh(task)
    assert task.skill_pins in (None, [])
    assert await _pins_of(task, "maker") == [("organize", 1)]


async def test_kind_words_stamp_nothing_and_the_lead_decides(db: AsyncSession, actor: Actor) -> None:
    """Kind language on an undialed goal is no dial: nothing is stamped from the words, the turn is the
    Lead's to decide, and nothing is pinned before it runs."""
    task = await _planning(db, actor, "build a kanban tracker for the move")
    view = await planning.draft_plan(db, task.id)
    await db.commit()

    await db.refresh(task)
    assert (task.deliverable, task.deliverable_source) == (None, None)
    [step] = view.steps
    assert step.agent == "lead", "the Lead decides, never a dial's one step"


async def test_an_undetected_goal_stamps_the_plans_own_kind(db: AsyncSession, actor: Actor) -> None:
    """Nothing named, nothing dialed: the Lead's validated plan is the path, stamped ("plan") so the chip
    can say what the task became."""
    task = await _planning(db, actor, "Plan the office move step by step")
    await lead_plans(task.id, _one("writer", "The move plan"))

    await db.refresh(task)
    assert (task.deliverable, task.deliverable_source) == ("document", "plan")


async def test_source_stamps_at_the_door_and_on_a_branch(db: AsyncSession, actor: Actor) -> None:
    """A wire dial is the user's ("user"); a fork inherits the pair VERBATIM - a plan-stamped
    parent kind must never become a fake user dial on the child."""
    dialed = await task_ops.create_task(db, actor, goal="g", deliverable="deck")
    assert (dialed.deliverable, dialed.deliverable_source) == ("deck", "user")

    undialed = await task_ops.create_task(db, actor, goal="just help")
    assert (undialed.deliverable, undialed.deliverable_source) == (None, None)

    inherited = await task_ops.create_task(
        db,
        actor,
        goal="branch",
        deliverable="document",
        deliverable_source="plan",
        parent_task_id=dialed.id,
        inherit_mode=True,
    )
    assert (inherited.deliverable, inherited.deliverable_source) == ("document", "plan")


async def test_a_followup_dial_stamps_user_and_rides_the_event(
    db: AsyncSession, actor: Actor, silent_dispatcher: object
) -> None:
    """A fresh-turn dial is the user's choice on the row AND on the TaskContinued frame -
    planning reads the event, never the stamp, to know which is which."""
    from prismoid.db.models.event import TaskEvent
    from prismoid.schemas.events import TaskContinued

    task = await task_ops.create_task(db, actor, goal="a memo on the move", clarify="never")
    task.transition(TaskStatus.PLANNING)
    task.transition(TaskStatus.RUNNING)
    task.transition(TaskStatus.DELIVERED)
    await db.commit()

    await task_ops.continue_task(db, actor, task.id, goal="make it a deck", deliverable="deck")
    await db.refresh(task)
    assert (task.deliverable, task.deliverable_source) == ("deck", "user")
    latest = await TaskEvent.latest_of_type(db, task.id, "task.continued")
    assert isinstance(latest, TaskContinued) and latest.deliverable == "deck"


async def test_a_user_pin_rides_every_step_before_the_ones_the_plan_names(
    db: AsyncSession, actor: Actor
) -> None:
    task = await task_ops.create_task(db, actor, goal="summarise this", skills=["report"], clarify="never")
    task.transition(TaskStatus.PLANNING)
    await db.commit()
    await lead_plans(task.id, _one("writer", "The summary", skills=["summarize", "report"]))

    await db.refresh(task)
    assert [p["name"] for p in task.skill_pins] == ["report"]
    assert [name for name, _ in await _pins_of(task, "writer")] == ["report", "summarize"]


async def test_a_format_implies_the_document_kind_at_the_door(db: AsyncSession, actor: Actor) -> None:
    """Picking "PDF" IS picking a document - and with any other kind it
    is a 422 naming the conflict, exactly like the fleet pairs."""
    task = await task_ops.create_task(db, actor, goal="the plan, please", deliverable_format="pdf")
    assert (task.deliverable, task.deliverable_source, task.deliverable_format) == (
        "document",
        "user",
        "pdf",
    )
    with pytest.raises(ValidationFailed):
        await task_ops.create_task(db, actor, goal="g", deliverable="deck", deliverable_format="pdf")


async def test_a_dialed_format_fast_paths_to_the_typesetter_with_its_skill(
    db: AsyncSession, actor: Actor
) -> None:
    """The md-only trap (a recorded boundary), closed: Document · PDF fast-paths to
    the agent that can MAKE a .pdf, with the pdf skill pinned deterministically and the
    format on the acceptance."""
    task = await task_ops.create_task(
        db, actor, goal="the office move plan", deliverable_format="pdf", clarify="never"
    )
    from prismoid.operations import execution

    await execution.advance(db, task.id, TaskStatus.PLANNING)
    view = await planning.draft_plan(db, task.id)
    await db.commit()

    [step] = view.steps
    assert step.agent == "maker", "a formatted document is the maker's"
    await db.refresh(task)
    assert [p["name"] for p in task.skill_pins] == ["pdf"]


async def test_a_followup_format_dial_stamps_and_rides_the_event(
    db: AsyncSession, actor: Actor, silent_dispatcher: object
) -> None:
    from prismoid.db.models.event import TaskEvent
    from prismoid.schemas.events import TaskContinued

    task = await task_ops.create_task(db, actor, goal="a memo on the move", clarify="never")
    task.transition(TaskStatus.PLANNING)
    task.transition(TaskStatus.RUNNING)
    task.transition(TaskStatus.DELIVERED)
    await db.commit()

    await task_ops.continue_task(db, actor, task.id, goal="same again as word doc", deliverable_format="docx")
    await db.refresh(task)
    assert (task.deliverable, task.deliverable_source, task.deliverable_format) == (
        "document",
        "user",
        "docx",
    )
    latest = await TaskEvent.latest_of_type(db, task.id, "task.continued")
    assert isinstance(latest, TaskContinued) and latest.deliverable_format == "docx"


# ── the review pins: every fixed behavior wears its test ─────────────────────────────────────


async def test_council_is_fleet_at_the_door(db: AsyncSession, actor: Actor) -> None:
    """ "council" is the legacy fleet wire value: the door NORMALIZES it to fleet
    before any guard, so the deliverable and format pairs are refused for it exactly as for
    "fleet" - pinned so the normalization can never quietly move below the guards."""
    from prismoid.db.models.feature import Feature
    from prismoid.db.models.project import Project
    from prismoid.schemas.plan import TaskMode

    project = Project(name="council-door", owner_actor_id=actor.id)
    db.add(project)
    await db.flush()
    db.add(Feature(name="prismoid:fleet", project_id=project.id, enabled=True))
    await db.flush()

    with pytest.raises(ValidationFailed, match="comparison table"):
        await task_ops.create_task(
            db, actor, goal="g", mode=TaskMode.COUNCIL, project_id=project.id, deliverable="deck"
        )
    with pytest.raises(ValidationFailed, match="comparison table"):
        await task_ops.create_task(
            db, actor, goal="g", mode=TaskMode.COUNCIL, project_id=project.id, deliverable_format="pdf"
        )


async def test_a_kind_changing_turn_dial_clears_the_stale_format_at_the_door(
    db: AsyncSession, actor: Actor, silent_dispatcher: object
) -> None:
    """A follow-up that dials a non-document kind clears the row's stale document format AT
    THE DOOR - the read-back must never serve the impossible (deck, pdf) pair through the
    clarifying window (or forever, if the turn dies before planning re-resolves)."""
    task = await task_ops.create_task(db, actor, goal="the plan", deliverable_format="pdf", clarify="never")
    task.transition(TaskStatus.PLANNING)
    task.transition(TaskStatus.RUNNING)
    task.transition(TaskStatus.DELIVERED)
    await db.commit()

    await task_ops.continue_task(db, actor, task.id, goal="make it slides instead", deliverable="deck")
    await db.refresh(task)
    assert (task.deliverable, task.deliverable_format) == ("deck", None)


async def test_a_refused_plan_says_so_on_the_record(db: AsyncSession, actor: Actor) -> None:
    """A Lead plan the graph rules refuse lands on the one-step floor, and the record wears why: the
    broken rule on `task.planned.route_why`, the person's words on `fallback` and the floor step (W2-G1)."""
    from prismoid.ai.capabilities import lead_check
    from prismoid.db.models.event import TaskEvent
    from prismoid.schemas.events import TaskPlanned

    task = await _planning(db, actor, "a memo on the vendors")
    lead_id = await lead_plans(task.id, _one("nobody", "The memo"))

    planned = await TaskEvent.latest_of_type(db, task.id, "task.planned")
    assert isinstance(planned, TaskPlanned)
    assert planned.fallback == lead_check.REFUSED_WHY
    assert planned.route_source == "lead" and planned.route_why.startswith("the Lead's plan fell to one step")
    assert "nobody" in planned.route_why
    [step] = [s for s in await PlanStep.for_task(db, task.id) if s.id != lead_id]
    assert StepSpec.model_validate(step.spec).fallback == planned.fallback


async def _planning(db: AsyncSession, actor: Actor, goal: str) -> Task:
    """A fresh task in planning, committed for the Lead's own sessions."""
    task = await task_ops.create_task(db, actor, goal=goal, clarify="never")
    task.transition(TaskStatus.PLANNING)
    await db.commit()
    return task


async def _pins_of(task: Task, agent: str) -> list[tuple[str, int]]:
    """The skills pinned on the task's step of `agent`, each with its version; every step's agents if none."""
    async with session_scope() as session:
        specs = [s.spec for s in await PlanStep.for_task(session, task.id)]
    made = [spec for spec in specs if spec["agent"] == agent]
    assert made, [spec["agent"] for spec in specs]
    return [(p["name"], p["version"]) for p in made[0]["skill_pins"]]


def _one(
    agent: str, title: str, *, skills: list[str] | None = None, produces: str | None = None
) -> PlanProposal:
    """The Lead's plan: one publishing step on `agent`, loading `skills`, making `produces` when it makes several."""
    step = ProposedStep.model_validate(
        {
            "label": "s1",
            "agent": agent,
            "title": title,
            "goal": title,
            "publishes": True,
            "skills": skills or [],
            "produces": produces,
        }
    )
    return PlanProposal(steps=[step], deliverable=DeliverableKind.FILES)
