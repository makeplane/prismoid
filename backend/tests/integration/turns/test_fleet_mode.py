# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Fleet mode behind its flag, and the plan it drafts (ADR-010).

Two things: `--mode fleet` while `prismoid:fleet` is off is a 409 at the door (not a task
that quietly plans a fleet nobody enabled), and a fleet task drafts the template - one
pinned writer step per eligible enabled model, plus the writer's table of them - never a model's
proposal.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.errors import FeatureDisabled
from prismoid.operations import planning
from prismoid.operations import tasks as task_ops
from prismoid.schemas.plan import TaskMode, TaskStatus
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def _an_actor(session: AsyncSession) -> Actor:
    return (await member(session, "Ana"))[0]


async def test_fleet_mode_off_is_a_409() -> None:
    async with session_scope() as session:
        # The dev database may carry a global enable row (that is how the owner runs fleets);
        # the test pins the OFF behaviour with a project-level override, which wins outright.
        from prismoid.db.models.feature import Feature
        from prismoid.db.models.project import Project

        actor = await _an_actor(session)
        project = Project(name="fleet-off", owner_actor_id=actor.id)
        session.add(project)
        await session.flush()
        session.add(Feature(name="prismoid:fleet", project_id=project.id, enabled=False))
        await session.flush()

        with pytest.raises(FeatureDisabled):
            await task_ops.create_task(
                session, actor, goal="raise prices?", mode=TaskMode.FLEET, project_id=project.id
            )


async def test_a_fleet_task_drafts_the_template() -> None:
    # A fleet task, constructed directly (the flag gate lives in create_task; draft_plan trusts the
    # task is a fleet because it was allowed to be one). draft_plan must produce the template.
    async with session_scope() as session:
        actor = await _an_actor(session)
        task = Task(
            id=uuid.uuid4(),
            goal="should we raise prices 10%?",
            mode=TaskMode.FLEET.value,
            status=TaskStatus.PLANNING.value,
            clarify="never",
            creator_actor_id=actor.id,
            criteria={},
            inputs=[],
            skill_pins=[],
            updated_at=utcnow(),
        )
        session.add(task)
        await session.flush()

        await planning.draft_plan(session, task.id)
        steps = await PlanStep.for_task(session, task.id)
        agents = [str(s.spec["agent"]) for s in steps]
        pins = [s.spec.get("model_ref") for s in steps]
        roles = [s.spec.get("lane_role") for s in steps]

    # One pinned writer per eligible enabled model (the dev catalog decides how many), then the
    # table - unpinned, because it is a role, not a seat.
    assert agents and all(a == "writer" for a in agents), agents
    assert roles[-1] == "fleet_table" and not any(roles[:-1]), roles
    writers = agents[:-1]
    assert writers, agents
    assert all(isinstance(p, str) and ":" in p for p in pins[:-1]), "every answer pins a catalog ref"
    assert len(set(pins[:-1])) == len(writers), "one seat per model - no ref twice"
    assert pins[-1] is None


async def test_chosen_seats_fly_and_only_those() -> None:
    """model_refs narrows the fleet to the creator's chosen seats: the plan pins
    exactly those refs - not the whole eligible catalog - and the table still sits."""
    async with session_scope() as session:
        from prismoid.operations import models as model_ops
        from prismoid.schemas.model_class import EXECUTOR_CLASSES

        catalog = await model_ops.catalog(session, enabled_only=True)
        flyable = [m.ref for m in catalog if any(m.serves(c) for c in EXECUTOR_CLASSES)]
        if not flyable:
            pytest.skip("no enabled executor model in the dev catalog")
        chosen = flyable[:1]

        actor = await _an_actor(session)
        task = Task(
            id=uuid.uuid4(),
            goal="should we raise prices 10%?",
            mode=TaskMode.FLEET.value,
            status=TaskStatus.PLANNING.value,
            clarify="never",
            creator_actor_id=actor.id,
            criteria={},
            inputs=[],
            skill_pins=[],
            model_refs=chosen,
            updated_at=utcnow(),
        )
        session.add(task)
        await session.flush()

        await planning.draft_plan(session, task.id)
        steps = await PlanStep.for_task(session, task.id)
        pins = [s.spec.get("model_ref") for s in steps]

    assert pins[:-1] == chosen, "exactly the chosen seats, in catalog order"
    assert pins[-1] is None, "the table is a role, not a seat"


async def test_seats_are_validated_at_the_door() -> None:
    """Like skill refs: a seat that is not an enabled executor model is a 422 at submit,
    and seats without fleet mode are a 422 - never a silently ignored setting."""
    from prismoid.errors import ValidationFailed

    async with session_scope() as session:
        from prismoid.db.models.feature import Feature
        from prismoid.db.models.project import Project

        actor = await _an_actor(session)
        project = Project(name="seat-validation", owner_actor_id=actor.id)
        session.add(project)
        await session.flush()
        session.add(Feature(name="prismoid:fleet", project_id=project.id, enabled=True))
        await session.flush()

        with pytest.raises(ValidationFailed):
            await task_ops.create_task(
                session,
                actor,
                goal="standard cannot take seats",
                mode=TaskMode.STANDARD,
                models=["anthropic:claude-haiku-4-5"],
            )

        with pytest.raises(ValidationFailed):
            await task_ops.create_task(
                session,
                actor,
                goal="unknown seat",
                mode=TaskMode.FLEET,
                project_id=project.id,
                models=["nowhere:not-a-model"],
            )


async def test_the_legacy_wire_value_still_lands_as_fleet() -> None:
    """Pre-rename clients say `council` on the wire; the row they get is a fleet. Compat armor -
    the one place the old word is allowed to appear, because this test is what keeps it working."""
    async with session_scope() as session:
        from prismoid.db.models.feature import Feature
        from prismoid.db.models.project import Project

        actor = await _an_actor(session)
        project = Project(name="legacy-alias", owner_actor_id=actor.id)
        session.add(project)
        await session.flush()
        session.add(Feature(name="prismoid:fleet", project_id=project.id, enabled=True))
        await session.flush()

        task = await task_ops.create_task(
            session, actor, goal="alias check", mode=TaskMode.COUNCIL, project_id=project.id
        )
        assert task.mode == TaskMode.FLEET.value
