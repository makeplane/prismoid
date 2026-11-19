# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Skill pinning on a task - submit-time resolution and plan-time injection.

The `--scratch` e2e run is proven live (the skill lands in a real running frame); this suite pins the
deterministic half against the database: a ref resolves to a concrete version *at submit time* (so a
running task's skills never shift under it), and planning copies those pins onto every executable
step (so the executor knows what to load). The model is not in the loop here - the Lead's run is
scripted - so these are fast and stable, and it is the pin *plumbing* under test, not the writer.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.plan import PlanStep
from prismoid.db.session import session_scope
from prismoid.errors import NotFound, ValidationFailed
from prismoid.operations import skills as ops
from prismoid.operations import tasks as task_ops
from prismoid.schemas.plan import DeliverableKind, PlanProposal, ProposedStep, TaskStatus
from tests.support.cast import member
from tests.support.lead_turn import lead_plans

pytestmark = pytest.mark.integration


async def _an_actor(session: AsyncSession) -> Actor:
    return (await member(session, "Scratcher"))[0]


async def test_resolve_ref_pins_to_a_concrete_version() -> None:
    async with session_scope() as session:
        await member(session, "Reader")  # skills resolve per workspace
        head = await ops.resolve_ref(session, "report")  # published head
        exact = await ops.resolve_ref(session, "report@1")

    assert (str(head.name), head.version) == ("report", 1)  # "report" resolves to the head's number
    assert (str(exact.name), exact.version) == ("report", 1)


async def test_resolve_ref_rejects_the_unresolvable() -> None:
    async with session_scope() as session:
        await member(session, "Reader")  # skills resolve per workspace
        with pytest.raises(NotFound):
            await ops.resolve_ref(session, "report@999")
        with pytest.raises(NotFound):
            await ops.resolve_ref(session, "nosuchskill")
        with pytest.raises(NotFound):  # @draft with no author in context is refused (author-only)
            await ops.resolve_ref(session, "report@draft")
        with pytest.raises(ValidationFailed):
            await ops.resolve_ref(session, "report@notanumber")


async def test_create_task_stores_resolved_pins() -> None:
    async with session_scope() as session:
        actor = await _an_actor(session)
        task = await task_ops.create_task(
            session, actor, goal="a scratch memo", skills=["report"], clarify="never"
        )
        pins = task.skill_pins

    assert pins == [{"name": "report", "version": 1}], "the ref is resolved to a version at submit time"


async def _planned(goal: str, skills: list[str]) -> list[list[dict[str, object]]]:
    """A task submitted with `skills`, its turn planned by the Lead as one writer step: each step's pins."""
    async with session_scope() as session:
        actor = await _an_actor(session)
        task = await task_ops.create_task(session, actor, goal=goal, skills=skills, clarify="never")
        task.transition(TaskStatus.PLANNING)
        await session.commit()
    step = ProposedStep(label="s1", agent="writer", title="Write", goal="write it", publishes=True)
    await lead_plans(task.id, PlanProposal(steps=[step], deliverable=DeliverableKind.FILES))
    async with session_scope() as session:
        return [s.spec.get("skill_pins") or [] for s in await PlanStep.for_task(session, task.id)]


async def test_a_plan_copies_the_tasks_pins_onto_every_executable_step() -> None:
    pins = await _planned("a scratch memo", ["report"])
    assert pins == [[{"name": "report", "version": 1}]] * 2, "the Lead's step and the writer's carry the pin"


async def test_no_skills_and_none_named_leaves_steps_unpinned() -> None:
    """Nothing pinned at submit and no skill named by the plan: no step loads one, and nothing guesses."""
    assert await _planned("an unpinned memo", []) == [[], []]
