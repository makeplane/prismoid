# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Planning - a turn's plan, put on the stream.

`operations` is the only composer: the Lead proposes (`ai/agents/lead.py`) and the rules dispose
(`ai/rules/plan.py`). Every new turn is a recipe's code-made plan or the Lead's one step
(`turn/route.route_turn`); a Lead's plan joins the turn through `turn/commit.land_plan`, and a re-plan after
a steer or an editor's ask is the Lead's replan seat (`turn/replan.py`). This file keeps the tail every
plan shares (`commit_plan`) and the light machinery calls (the namer, memory extraction); what plans
share around them lives beside it: the step writes, pins and views in `planning_steps`, the fleet recipe,
the floor, the named hosts and the re-plan journal writes in `planning_turns`, and the looks in
`turn/recipes`.
"""

from __future__ import annotations

import uuid

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.agents.base import build
from prismoid.ai.deps import TaskDeps
from prismoid.db.base import utcnow
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.errors import NotFound
from prismoid.operations import context as context_ops
from prismoid.operations import machines, steering
from prismoid.operations import models as model_ops
from prismoid.operations import routing as routing_ops
from prismoid.operations import usage as usage_ops
from prismoid.operations.planning_steps import pin_bots, pin_skills, plan_view, turn_ask, view_of, write_steps
from prismoid.operations.planning_turns import Routed, pin_dialed_skill, stamp_plan_kind
from prismoid.operations.turn.recipes import gate_visual_steps
from prismoid.schemas.events import TaskPlanned, TaskRenamed
from prismoid.schemas.ids import ProjectId, TaskId
from prismoid.schemas.inputs import visible
from prismoid.schemas.outputs import TaskTitle
from prismoid.schemas.plan import PlanView, StepState, TaskStatus


async def draft_plan(session: AsyncSession, task_id: uuid.UUID) -> PlanView:
    """Plan the task, write the steps, emit `task.planned`. One transaction."""
    task = await session.get(Task, task_id)
    if task is None:
        raise NotFound("no such task")

    if task.state is TaskStatus.PLANNING:
        pass  # the expected state
    elif task.state is TaskStatus.RUNNING:
        return await plan_view(session, task)  # a replayed effect - already planned
    # The epoch this plan is drafted under, read before any model call: every steer
    # up to it is in the transcript the Lead reads, so the plan applies them; one that lands
    # later is past it, and the first frontier boundary re-plans for it.
    drafted = task.plan_epoch

    # **A continuation plans the NEW message**. Turn N keeps the verified work - DONE
    # steps stand and their artifacts mount - and re-drafts from the follow-up: the unfinished
    # remnants of a failed or cancelled prior turn drop exactly as a replan drops them, and the
    # new steps' ordinals continue past the kept ones so the plan still reads top to bottom.
    # THIS turn's explicit dials come from its event, never from the row's stamp.
    ask = await turn_ask(session, task)
    base_ordinal = 0
    if task.turn > 1:
        kept = [s for s in await PlanStep.for_task(session, task.id) if s.step_state is StepState.DONE]
        await PlanStep.delete_unfinished(session, task.id)
        base_ordinal = max((s.ordinal for s in kept), default=-1) + 1

    from prismoid.operations.turn import route as turn_route  # a cycle: the route reads the engine's mounts

    # Every new turn: the recipe intake names, else the Lead's one step (ADR-092).
    routed = await turn_route.route_turn(session, task, ask)
    return await commit_plan(session, task, routed, base_ordinal=base_ordinal, drafted=drafted)


async def commit_plan(
    session: AsyncSession,
    task: Task,
    routed: Routed,
    *,
    base_ordinal: int,
    drafted: int,
    record_context: bool = True,
) -> PlanView:
    """The tail every lane shares: the plan-stamp, the pins, the gates, the writes, and the
    routed record - in the caller's transaction. `drafted` is the epoch planning read: the steps
    carry it, and the plan applies every steer up to it."""
    steps, edges, criteria = routed.steps, routed.edges, routed.criteria
    lane = routed.lane

    stamp_plan_kind(task, lane, steps)
    await pin_dialed_skill(session, task, lane)
    steps = pin_skills(steps, task)  # the task's requested skills, onto every executable step
    steps = await pin_bots(steps, session, task)  # each bot step, pinned to a version
    steps = gate_visual_steps(steps)  # file deliverables get the look judged before done

    # The provider call is behind us - now the writes, in the same commit as the plan itself.
    if record_context and routed.pack is not None:
        await context_ops.record(session, task, routed.pack)
    rows = await write_steps(session, task, steps, edges, base_ordinal=base_ordinal, epoch=drafted)
    task.criteria = criteria.model_dump(mode="json")
    await session.flush()

    view = view_of(rows, criteria)
    need = machines.plan_need_for(task, (spec for _, spec in steps))
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        TaskPlanned(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            plan=view,
            route=lane,
            route_source=routed.lane_source,
            route_why=routed.lane_why[:200],
            machine=need.need,
            machine_why=need.why,
            fallback=routed.fallback,
        ),
    )
    await steering.apply_planned(session, task, drafted)
    return view


async def run_light_agent(session: AsyncSession, task: Task, name: str, brief: str) -> object:
    """Run a machinery agent (namer, memory_extract) with usage metered through the same deps.
    The catalog read commits before the model call, so no snapshot or pool connection idles across it."""

    deps = TaskDeps(
        task_id=TaskId(task.id),
        project_id=ProjectId(task.project_id) if task.project_id else None,
        emit_usage=usage_ops.emitter(session),  # a machinery call is a provider call
    )
    catalog, order = await model_ops.routed_catalog(session)
    runner = build(name, deps, catalog, order=order, settings=await routing_ops.settings_for(session))
    await session.commit()  # nothing pending - just release the snapshot + connection
    return (await runner.run(brief)).output


TITLE_MAX = 60  # the hard lip: fits a breadcrumb, a sidebar row, and a board card


async def generate_title(session: AsyncSession, task_id: uuid.UUID) -> str | None:
    """Mint the task's display title once, from the typed instruction only, and only into silence.
    A set title (the person's rename) always wins; a namer that errors names nothing (`task.renamed` when it lands)."""
    task = await session.get(Task, task_id)
    if task is None or task.title:
        return None

    try:
        ask = visible(task.goal)[:2000]
        output = await run_light_agent(session, task, "namer", f"Name this task.\n\nThe ask:\n{ask}")
        title = output.title.strip().strip("\"'") if isinstance(output, TaskTitle) else ""
    except Exception:
        logfire.exception("namer failed for {task_id}; keeping the goal-derived label", task_id=str(task_id))
        return None

    title = " ".join(title.split())[:TITLE_MAX].rstrip(" .,:;-")
    if not title:
        return None

    task.title = title
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        TaskRenamed(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            title=title,
        ),
    )
    logfire.info("task {task_id} titled: {title}", task_id=str(task_id), title=title)
    return title
