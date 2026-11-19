# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A Lead's recipe step becomes its code-made shape at commit, and the shape runs in order (ADR-092).

A scatter recipe commits as the map step: the map's wave runs, then its synthesis, then whatever the plan
put after the recipe step. A long-form recipe commits as the outline step, the row sized for it: its parts
run in order, then the assembly, then what came after. The order is the real promotion's
(`finish.promote_dependents`), walked by hand with every step's run stubbed out.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest

from prismoid.db.models.plan import PlanEdge as PlanEdgeRow
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import longform as longform_ops
from prismoid.operations import swarm as swarm_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations.execution import finish
from prismoid.schemas.longform import OutlineResult, SectionOutline
from prismoid.schemas.plan import (
    DeliverableKind,
    PlanProposal,
    ProposedStep,
    RecipeKind,
    StepRecipe,
    StepSpec,
    StepState,
    TaskStatus,
)
from prismoid.schemas.swarm import ScatterResult, SeatBrief
from tests.support.cast import member
from tests.support.lead_turn import lead_plans

pytestmark = pytest.mark.integration

SWEEP = "Find 12 open-source vector databases with their licenses"
HANDBOOK = "Write a 20-page onboarding handbook for new support engineers"

_AFTER = ProposedStep(
    label="s2",
    agent="writer",
    title="The cover note",
    goal="Write a one-paragraph cover note for what came before",
    depends_on=["s1"],
    publishes=True,
)


def _plan(recipe: StepRecipe, goal: str, agent: str, *, then: bool = True) -> PlanProposal:
    first = ProposedStep(
        label="s1", agent=agent, title="The recipe step", goal=goal, recipe=recipe, publishes=True
    )
    return PlanProposal(steps=[first, *([_AFTER] if then else [])], deliverable=DeliverableKind.FILES)


async def _planned(goal: str, plan: PlanProposal) -> uuid.UUID:
    """A task whose turn the Lead planned with `plan`, committed through the real step run."""
    async with session_scope() as session:
        actor, _ = await member(session, "Planner")
        await session.flush()
        task = await task_ops.create_task(session, actor, goal=goal, clarify="never")
        task.transition(TaskStatus.PLANNING)
        await session.commit()
        task_id = task.id
    lead_id = await lead_plans(task_id, plan)
    async with row_scope(Task, task_id) as session:  # the Lead's own step is done: the plan's steps remain
        lead = await session.get(PlanStep, lead_id)
        assert lead is not None and lead.step_state is StepState.DONE
    return task_id


async def _steps(task_id: uuid.UUID) -> dict[uuid.UUID, tuple[str, StepSpec, StepState]]:
    """Every step of the plan after the Lead's: its role (the lane role, or its title), spec and state."""
    async with session_scope() as session:
        return {
            r.id: ((spec := StepSpec.model_validate(r.spec)).lane_role or spec.title, spec, r.step_state)
            for r in await PlanStep.for_task(session, task_id)
            if r.spec.get("agent") != "lead"
        }


async def _upstream(task_id: uuid.UUID, step: uuid.UUID) -> set[str]:
    """The roles a step waits on."""
    steps = await _steps(task_id)
    async with session_scope() as session:
        upstream = [
            e.from_step_id for e in await PlanEdgeRow.for_task(session, task_id) if e.to_step_id == step
        ]
    return {steps[sid][0] for sid in upstream if sid in steps}


def _one(steps: dict[uuid.UUID, tuple[str, StepSpec, StepState]], role: str) -> uuid.UUID:
    (found,) = [sid for sid, (r, _, _) in steps.items() if r == role]
    return found


async def _finish(task_id: uuid.UUID, done: Sequence[uuid.UUID]) -> None:
    """Each step in `done` finishes in turn, its dependents promoted by the real promotion."""
    for step_id in done:
        async with row_scope(Task, task_id) as session:
            task, step = await session.get(Task, task_id), await session.get(PlanStep, step_id)
            assert task is not None and step is not None
            step.state = StepState.DONE.value
            await session.flush()
            await finish.promote_dependents(session, task, step)
            await session.commit()


def _ready(steps: dict[uuid.UUID, tuple[str, StepSpec, StepState]]) -> set[str]:
    return {role for role, _, state in steps.values() if state is StepState.READY}


async def test_a_scatter_recipe_runs_its_map_then_its_wave_then_synthesis_then_the_rest() -> None:
    task_id = await _planned(
        SWEEP, _plan(StepRecipe(kind=RecipeKind.SCATTER, target_count=12), SWEEP, "researcher")
    )
    steps = await _steps(task_id)
    map_id, note_id = _one(steps, "scatter_map"), _one(steps, "The cover note")
    map_spec = steps[map_id][1]
    assert map_spec.contract is not None and map_spec.contract.goal == SWEEP
    assert any("target_count is 12" in line for line in map_spec.contract.acceptance)
    assert _ready(steps) == {"scatter_map"}

    seats = [
        SeatBrief(
            id=f"g0{i}", goal=f"Sweep source {i}", not_here=f"every source but {i}", expected="a database"
        )
        for i in (1, 2)
    ]
    mapped = ScatterResult(
        objective=SWEEP, entity="a database", target_count=12, record_fields=["license"], seats=seats
    )
    async with row_scope(Task, task_id) as session:
        task, row = await session.get(Task, task_id), await session.get(PlanStep, map_id)
        assert task is not None and row is not None
        await swarm_ops.expand_after_scatter(session, task, row, mapped)
        await session.commit()
    await _finish(task_id, [map_id])
    steps = await _steps(task_id)
    synthesis = _one(steps, "synthesis")
    assert _ready(steps) == {"gather"}, "the wave runs first"
    assert await _upstream(task_id, note_id) == {"synthesis"}, (
        "what followed the recipe waits on its whole shape"
    )
    told = steps[synthesis][1].contract
    assert told is not None and told.goal.startswith(SWEEP), "the synthesis serves the step's own goal"

    await _finish(task_id, [sid for sid, (role, _, _) in steps.items() if role == "gather"])
    assert _ready(await _steps(task_id)) == {"synthesis"}
    await _finish(task_id, [synthesis])
    assert _ready(await _steps(task_id)) == {"The cover note"}


def _outline() -> OutlineResult:
    return OutlineResult(
        filename="handbook",
        title="The support handbook",
        continuity_bible="Plain words, second person.",
        summary="A handbook for new support engineers.",
        sections=[
            SectionOutline(id=f"s{i:02d}", title=f"Part {i}", brief=f"Cover part {i}.", target_words=2000)
            for i in (1, 2, 3)
        ],
    )


async def test_a_longform_recipe_runs_its_outline_then_its_parts_in_order_then_the_rest() -> None:
    task_id = await _planned(
        HANDBOOK, _plan(StepRecipe(kind=RecipeKind.LONGFORM, target_pages=20), HANDBOOK, "writer")
    )
    async with session_scope() as session:
        task = await session.get(Task, task_id)
        assert task is not None and (task.scale, task.target_pages) == ("extended", 20), "sized like the dial"
    steps = await _steps(task_id)
    outline_id, note_id = _one(steps, "outline"), _one(steps, "The cover note")
    assert str(steps[outline_id][1].agent) == "writer" and _ready(steps) == {"outline"}
    assert await _upstream(task_id, note_id) == {"outline"}

    async with row_scope(Task, task_id) as session:
        task, row = await session.get(Task, task_id), await session.get(PlanStep, outline_id)
        assert task is not None and row is not None
        await longform_ops.expand_after_outline(session, task, row, _outline())
        await session.commit()
    await _finish(task_id, [outline_id])
    steps = await _steps(task_id)
    parts = sorted(
        (sid for sid, (role, _, _) in steps.items() if role == "section"), key=lambda s: steps[s][1].title
    )
    assembly = _one(steps, "assembly")
    assert len(parts) == 3 and await _upstream(task_id, note_id) == {"assembly"}
    for part in parts:
        ready = [sid for sid, (_, _, state) in (await _steps(task_id)).items() if state is StepState.READY]
        assert ready == [part], "one part at a time, in order"
        await _finish(task_id, [part])
    assert _ready(await _steps(task_id)) == {"assembly"}
    await _finish(task_id, [assembly])
    assert _ready(await _steps(task_id)) == {"The cover note"}
