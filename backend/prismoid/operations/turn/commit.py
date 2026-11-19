# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A Lead run's ending, committed: each ending's rows staged in the step's completion transaction (ADR-092).

The ladder calls `commit_ending` after fence 1 and before the step's DONE, and the caller's one commit
lands the ending's rows, the DONE frame wearing `ending` and the narration together: a crash before it
leaves none of them, and the re-run's ending stages its own. A reply writes nothing more (the express
shape); a plan lands as `plan.edited{lead}` and a `task.planned` wearing `route_source=lead` and the
line the person read, each recipe step expanded into its code-made shape; a proposal is the card under the answer; an
unattended run's result for its owner is the line the turn delivers, its DONE frame wearing `findings`.
An ask never reaches here: it parks the step on the input gate and the answer starts a fresh Lead run
(`execution/lead_rung.py`); nor does an unattended failure, which fails the step and its task
(`handlers/fence.blocked`).
"""

from __future__ import annotations

from dataclasses import dataclass

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.agents.base import roster
from prismoid.ai.agents.bot_compile import seat_table
from prismoid.ai.capabilities import lead_check
from prismoid.ai.capabilities.lead_check import LONGFORM_PAGES
from prismoid.ai.rules.plan import InvalidPlan, Plan
from prismoid.db.base import utcnow
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.operations import context as context_ops
from prismoid.operations import planning
from prismoid.operations import swarm as swarm_ops
from prismoid.operations.planning_steps import TurnAsk, bot_roster, turn_ask
from prismoid.operations.planning_turns import Routed, accept_proposal, record_lead
from prismoid.operations.turn import intake as turn_intake
from prismoid.operations.turn import route as turn_route  # a module, so a route-first import cycle resolves
from prismoid.schemas import sizing
from prismoid.schemas.context import ContextPack
from prismoid.schemas.conversation import DocumentFormat
from prismoid.schemas.events import TaskProposed
from prismoid.schemas.ids import ProjectId, StepId, TaskId
from prismoid.schemas.inputs import InputSet, TaskInput
from prismoid.schemas.lead import Decline, Deliver, ProposeTask, Reply, SubmitPlan
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import PlanProposal, ProposedStep, RecipeKind, StepRecipe

# The ending a committed Lead run took: every one but the ask, which parks, and a failure, which fails.
Committing = Reply | SubmitPlan | ProposeTask | Deliver | Decline


@dataclass(frozen=True, slots=True)
class Committed:
    """What the step says once its ending is staged: its narration, and whether that line is only on
    the thread while the plan's steps deliver the turn (`interim`)."""

    narration: str
    interim: bool = False


async def commit_ending(session: AsyncSession, task: Task, step: PlanStep, ending: Committing) -> Committed:
    """Stage `ending`'s rows in the caller's transaction (claims seqs: the task row is held to its commit)."""
    match ending:
        case SubmitPlan():
            return await land_plan(session, task, step, ending)
        case ProposeTask():
            await propose(
                session, task, step, goal=ending.goal, deliverable=ending.deliverable.value, fmt=ending.format
            )
            return Committed(ending.answer)
        case Deliver():
            return Committed(ending.result)
        case Reply():
            return Committed(ending.text)
        case Decline():
            return Committed(ending.answer)


async def propose(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    *,
    goal: str,
    deliverable: str | None,
    fmt: DocumentFormat | None = None,
    why: str = "",
) -> None:
    """The card: one durable `task.proposed` under the reply, riding the step's commit. Start is a
    follow-up with the deliverable dial set; Not now is nothing."""
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        TaskProposed(
            seq=seq,
            task_id=TaskId(task.id),
            step_id=StepId(step.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            goal=goal.strip()[:4000] or "(no goal)",
            deliverable=deliverable,
            deliverable_format=fmt,
            why=why[:300],
            turn=task.turn,
        ),
    )


async def land_plan(session: AsyncSession, task: Task, step: PlanStep, ending: SubmitPlan) -> Committed:
    """The Lead's plan joins the turn in the caller's transaction: the plan edit, the steps after every
    existing one, and the routed record. Returns the line the person reads: the plan's, or its fallback's."""
    # The epoch read from the row: a steer the run took since the step loaded the task has moved it.
    await session.refresh(task, attribute_names=["plan_epoch"])
    drafted = task.plan_epoch
    pack = await context_ops.readable_pack(session, task)  # the turn's frozen pack, through the fence
    routed, say = await _routed(session, task, ending, await turn_ask(session, task), pack)
    await record_lead(session, task, say, step)
    base = max((s.ordinal for s in await PlanStep.for_task(session, task.id)), default=-1) + 1
    await planning.commit_plan(
        session, task, routed, base_ordinal=base, drafted=drafted, record_context=False
    )
    logfire.info(
        "lead: task {t} turn {n} planned {k} steps", t=str(task.id), n=task.turn, k=len(routed.steps)
    )
    return Committed(say, interim=True)


async def _routed(
    session: AsyncSession, task: Task, ending: SubmitPlan, ask: TurnAsk, pack: ContextPack | None
) -> tuple[Routed, str]:
    """The Lead's plan accepted, or the one step that keeps its ending when it breaks a rule, and the line
    the person reads, which says so; the record names why."""
    inputs = InputSet(items=[TaskInput.model_validate(i) for i in task.inputs])
    say, fallback = ending.say, ending.plan.fallback
    try:
        plan = await accepted_lead_plan(session, task, ending.plan, ask, inputs)
        why = f"the Lead's plan fell to one step: {fallback}" if fallback else say
    except InvalidPlan as bad:
        logfire.warn("lead plan refused for {t}: {why}", t=str(task.id), why=str(bad))
        dialed = _dialed(task, ask)
        floor = lead_check.fallback(
            ending.plan,
            seat_table(()),
            dialed=dialed,
            doc_format=task.deliverable_format,
            request=ask.message or task.goal,
            why=lead_check.REFUSED_WHY,
        )
        plan = await accepted_lead_plan(session, task, floor, ask, inputs)
        say, fallback, why = lead_check.FLOOR_SAY, floor.fallback, f"the Lead's plan fell to one step: {bad}"
    steps, edges, criteria = plan
    if fallback:  # the floor's step wears why it runs in the Lead's plan's place
        steps = [(sid, spec.model_copy(update={"fallback": fallback[:300]})) for sid, spec in steps]
    routed = Routed(
        steps=steps,
        edges=edges,
        criteria=criteria,
        lane="standard",
        lane_source="lead",
        lane_why=why[:200],
        pack=pack,
        fallback=fallback[:300],
    )
    return routed, say


async def accepted_lead_plan(
    session: AsyncSession, task: Task, proposal: PlanProposal, ask: TurnAsk, inputs: InputSet
) -> Plan:
    """A Lead plan held to the graph rules: a person's dialed kind binds its delivery and their dialed size
    bands its typed steps, each recipe step is expanded into its code-made shape and a long-form recipe's
    size lands on the row. Raises `InvalidPlan`."""
    plan = await accept_proposal(
        session,
        task,
        proposal,
        plannable=[*roster(plannable=True), *await bot_roster(session, task)],
        inputs=inputs,
        deliverable=_dialed(task, ask),
        ask=ask.message,
        recipes=await _recipe_shapes(session, task, proposal, inputs),
        size=turn_intake.turn_size(await turn_intake.read_rows(session, task)),
    )
    _stamp_size(task, proposal)
    return plan


def _dialed(task: Task, ask: TurnAsk) -> ArtifactType | None:
    """The kind a person dialed: this turn's dial, else the row's user stamp."""
    dialed = ask.deliverable or task.user_deliverable
    return ArtifactType(dialed) if dialed else None


async def _recipe_shapes(
    session: AsyncSession, task: Task, plan: PlanProposal, inputs: InputSet
) -> dict[str, Plan]:
    """Each recipe step's code-made shape, by its label (`rules.plan.validate` puts it in the step's place)."""
    return {
        step.label: await _recipe_shape(session, task, step, step.recipe, inputs)
        for step in plan.steps
        if step.recipe is not None
    }


async def _recipe_shape(
    session: AsyncSession, task: Task, step: ProposedStep, recipe: StepRecipe, inputs: InputSet
) -> Plan:
    """The code-made steps a recipe step becomes: the scatter's map, or the long-form lane's outline."""
    if recipe.kind is RecipeKind.SCATTER:
        return swarm_ops.scatter_plan(step.goal, target_count=recipe.target_count)
    pages = recipe.target_pages or LONGFORM_PAGES[0]
    bucket = sizing.bucket_for_units(ArtifactType.DOCUMENT, pages)
    return await turn_route.longform_shape(
        session, task, step.goal, inputs, kind=ArtifactType.DOCUMENT, bucket=bucket, target_pages=pages
    )


def _stamp_size(task: Task, plan: PlanProposal) -> None:
    """A long-form recipe's size on the row, as the size dial's is: the outline's expansion and the cost
    gate read it."""
    pages = next(
        (
            s.recipe.target_pages
            for s in plan.steps
            if s.recipe is not None and s.recipe.kind is RecipeKind.LONGFORM and s.recipe.target_pages
        ),
        None,
    )
    if pages is not None:
        task.scale, task.target_pages = sizing.bucket_for_units(ArtifactType.DOCUMENT, pages).value, pages
