# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A re-plan of a turn the Lead planned: the Lead again, in its replan seat (ADR-064, ADR-092).

A steer accepted after the Lead's plan committed reaches the steps not yet started at the next frontier
boundary, and an editor's replan of a turn comes here as a steer in their words. The Lead runs once more, ending only in `revise_plan` or `keep_plan`, and reads the thread, what
the turn has done, the steps not started and the steers since. It drafts first with no lock held; then,
under the task row lock and against a re-read frontier, a revision replaces the steps not started (the
done work stands and feeds the new roots, `plan.edited{replan}`) and a keep leaves them, carrying the
steers from then on. Either way each steer is `applied{replan}`. Any error, a frontier that moved, a
retracted steer or a revision the graph rules refuse keeps the plan, whose steps take the steer when
they start. A turn the Lead did not plan (a recipe, a fleet) keeps its plan, and its steps take the steer
through their contracts.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.agents import base as agents_base
from prismoid.ai.capabilities import lead_check
from prismoid.ai.context.turn import replan_note
from prismoid.ai.deps import TaskDeps
from prismoid.ai.rules.plan import InvalidPlan, Plan
from prismoid.conf import settings
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.steer import Steer
from prismoid.db.models.task import Task
from prismoid.errors import Conflict
from prismoid.operations import allowance, live, steering
from prismoid.operations import models as model_ops
from prismoid.operations import routing as routing_ops
from prismoid.operations import thread as thread_ops
from prismoid.operations import usage as usage_ops
from prismoid.operations.execution import lead_rung
from prismoid.operations.execution.express import express_ambient, express_prompt
from prismoid.operations.planning_steps import pin_bots, pin_skills, turn_ask
from prismoid.operations.planning_turns import (
    IN_FLIGHT,
    UNSTARTED,
    DoneWork,
    done_work,
    frontier_moved,
    replace_unstarted,
)
from prismoid.operations.turn import commit as turn_commit
from prismoid.operations.turn.recipes import gate_visual_steps
from prismoid.schemas.contract import Criteria
from prismoid.schemas.events import TaskPlanned
from prismoid.schemas.ids import ProjectId, TaskId
from prismoid.schemas.inputs import InputSet, TaskInput
from prismoid.schemas.lead import KeepPlan, PlanRevision
from prismoid.schemas.plan import StepSpec, StepState, TaskStatus
from prismoid.schemas.steering import SafePoint, SteerNote, SteerState, replanning_label


@dataclass(frozen=True, slots=True)
class _Frontier:
    """What the re-plan drafted for, read before the model: re-checked under the lock before any write."""

    task_id: uuid.UUID
    turn_epoch: int
    drafted: int  # the plan epoch the draft read
    unstarted: tuple[uuid.UUID, ...]


@dataclass(frozen=True, slots=True)
class Replanned:
    """What a re-plan did: `moved` when the workflow must read the frontier again, `said` how the narrated
    phase ends ("" when the Lead was never asked)."""

    moved: bool = False
    said: str = ""


KEPT: Final = "Kept the plan"
# Why a re-plan that did not finish kept the plan, in the person's words.
UNFINISHED: Final = "re-planning did not finish; the steps not started follow your steer"

# A boundary with nothing to re-plan: the frontier stands and nothing is said.
UNMOVED: Final = Replanned()


async def at_boundary(session: AsyncSession, task: Task, taken: Sequence[SteerNote]) -> Replanned:
    """The boundary's re-plan for `taken`: the Lead's replan seat when the Lead planned this turn (the latest
    `task.planned` wears `route_source=lead`); any other plan stands. The caller commits, then says `said`."""
    planned = await TaskEvent.latest_of_type(session, task.id, "task.planned")
    if isinstance(planned, TaskPlanned) and planned.route_source == lead_rung.LEAD:
        return await lead_replan(session, task, taken)
    return UNMOVED


async def by_editor(session: AsyncSession, actor: Actor, task_id: uuid.UUID, reason: str) -> tuple[int, int]:
    """An editor's replan: their words land as their accepted steer (`steering.steer`), then the Lead's replan
    seat takes the turn's steers; a settled task, a step mid-wave or nothing to re-plan is a 409 before both."""
    task = await Task.get_for(session, task_id, actor)  # scope first: a replan is not a side door
    await steering.require_project_editor(session, task, actor)  # viewers watch
    # The Lead's replan is a model call: a spent allowance is refused first, like every door (ADR-103).
    await allowance.require(session, task.workspace_id, actor=actor)
    if task.is_terminal():
        raise Conflict(f"cannot replan a {task.status} task - send a follow-up to continue it")
    steps_now = await PlanStep.for_task(session, task.id)
    if any(s.step_state is StepState.RUNNING or s.waiting_out_a_pace() for s in steps_now):
        raise Conflict("cannot replan while a step is running or waiting to try again - replan at a boundary")
    if not _replannable(steps_now):
        raise Conflict(_NOTHING)
    await steering.steer(session, actor, task_id, reason)  # commits: kept or not, the steps take the words
    taken = await steering.pending(session, task)
    # Read before the re-plan: a kept plan rolls the session back, which expires the row.
    turn, anchor, project = task.turn, task.event_seq, ProjectId(task.project_id) if task.project_id else None
    replanned = await lead_replan(session, task, taken, actor=actor) if taken else UNMOVED
    if not replanned.said:
        raise Conflict(_NOTHING)
    await session.commit()
    voice = live.phase_narrator(TaskId(task_id), project_id=project, after_seq=anchor)
    await voice(
        "end", replanned.said, agent=lead_rung.LEAD
    )  # after the commit: the record holds what it says
    if replanned.moved and replanned.said == KEPT:
        raise Conflict(
            "the plan moved while it was re-drafted (a step started, or it was rewritten) - nothing replaced;"
            " your steer stands and its steps take it as they start"
        )
    return turn, len(await PlanStep.for_task(session, task_id))


_NOTHING: Final = (
    "nothing in this plan can be re-planned now - its steps have started or take it as they start"
)


def _replannable(steps_now: Sequence[PlanStep]) -> list[PlanStep]:
    """The steps not started a re-plan may replace; none mid-wave, and a recipe's steps take a steer at start."""
    unstarted = [s for s in steps_now if s.step_state in UNSTARTED]
    if any((s.spec or {}).get("lane_role") for s in unstarted) or any(
        s.step_state in IN_FLIGHT for s in steps_now
    ):
        return []
    return unstarted


async def lead_replan(
    session: AsyncSession, task: Task, taken: Sequence[SteerNote], *, actor: Actor | None = None
) -> Replanned:
    """Revise or keep the steps not started for `taken` (`actor`: the editor whose words they are, None for
    steers); the caller commits, then says `Replanned.said`."""
    steps_now = await PlanStep.for_task(session, task.id)
    unstarted = _replannable(steps_now)
    if not unstarted:
        return Replanned()
    frontier = _Frontier(task.id, task.turn_epoch, task.plan_epoch, tuple(s.id for s in unstarted))
    say = live.phase_narrator(
        TaskId(task.id),
        project_id=ProjectId(task.project_id) if task.project_id else None,
        after_seq=task.event_seq,
    )
    await say("start", replanning_label(taken), agent=lead_rung.LEAD)
    try:
        return await _replanned(session, task, taken, steps_now, frontier, actor=actor)
    except Exception:
        # Fail-soft: a boundary never fails the task it steers; the plan stands and its steps take the steer.
        await session.rollback()
        logfire.exception("the Lead's re-plan of {t} failed; the plan stands", t=str(frontier.task_id))
        moved = await _moved_after_error(session, frontier)
        return Replanned(moved=moved, said=KEPT if moved else f"{KEPT}: {UNFINISHED}")


async def _replanned(
    session: AsyncSession,
    task: Task,
    taken: Sequence[SteerNote],
    steps_now: Sequence[PlanStep],
    frontier: _Frontier,
    *,
    actor: Actor | None,
) -> Replanned:
    """The replan run, then its ending landed under the lock: a keep, or a revision the rules accept."""
    done = await done_work(session, steps_now, frontier.turn_epoch)
    unstarted = [s for s in steps_now if s.id in frontier.unstarted]
    ending = await _run(session, task, taken, done, unstarted)
    if isinstance(ending, KeepPlan):
        return await _keep(session, taken, frontier, ending.reason)
    try:
        plan = await _accepted(session, task, ending)
    except InvalidPlan as bad:
        logfire.warn("the Lead's revision of {t} was refused: {why}", t=str(task.id), why=str(bad))
        return await _kept(session, frontier, moved=False, why=lead_check.REVISION_KEPT)
    moved, task = await frontier_moved(session, frontier.task_id, frontier.turn_epoch, frontier.unstarted)
    if moved:
        return await _kept(session, frontier, moved=True)
    states = await Steer.states_of(session, frontier.task_id, [n.command_id for n in taken])
    if any(state == SteerState.RETRACTED.value for state in states.values()):
        return await _kept(session, frontier, moved=False)  # words nobody stands behind
    await replace_unstarted(
        session,
        task,
        plan,
        taken=taken,
        steps_now=steps_now,
        unstarted_ids=frontier.unstarted,
        feeding=done.feeding,
        drafted=frontier.drafted,
        actor=actor,
    )
    return Replanned(moved=True, said=ending.say)


async def _run(
    session: AsyncSession,
    task: Task,
    taken: Sequence[SteerNote],
    done: DoneWork,
    unstarted: Sequence[PlanStep],
) -> PlanRevision | KeepPlan:
    """The Lead in its replan seat: no tools, no stream; the snapshot is released before the model call."""
    deps = TaskDeps(
        task_id=TaskId(task.id),
        project_id=ProjectId(task.project_id) if task.project_id else None,
        emit_usage=usage_ops.emitter(session, own_transaction=True),  # spend survives a kept plan
        replanning=True,
    )
    catalog = await model_ops.catalog(session, enabled_only=True)
    order = await routing_ops.order_for(session)
    asked = await routing_ops.settings_for(session)
    turn = await lead_rung.brief(
        session, task, deps, asked=False, catalog=catalog, order=order, settings=asked
    )
    transcript = await thread_ops.render_thread(session, task, budget_tokens=thread_ops.EXPRESS_THREAD_TOKENS)
    tail = f"{turn.roster}\n\n{(await express_ambient(session, task)).rstrip()}\n\n"
    prompt = express_prompt(
        _brief(done, unstarted),
        transcript,
        ambient=tail + replan_note(taken, turn.facts),
    )
    runner = agents_base.build(
        lead_rung.LEAD, deps, catalog, order=order, settings=asked, quality=task.quality
    )
    await session.commit()  # nothing pending: release the snapshot and the connection before the model
    wall = runner.envelope.narrowed(float(settings.worker.express_timeout_s))
    output = (await wall.within(deps, runner.run(prompt))).output
    if not isinstance(output, PlanRevision | KeepPlan):  # pragma: no cover - the seat's endings pin it
        raise TypeError(f"the replan seat ended in a {type(output).__name__}")
    return output


def _brief(done: DoneWork, unstarted: Sequence[PlanStep]) -> str:
    """The plan as the replan run reads it: the done work with its files, then the steps not started."""
    made = "\n".join(done.lines) or "- nothing yet"
    waiting: list[str] = []
    for step in unstarted:
        spec = StepSpec.model_validate(step.spec)
        goal = f": {spec.contract.goal}" if spec.contract is not None else ""
        waiting.append(f"- {step.title} ({spec.agent}){goal}")
    return (
        "# The plan so far\n\nDone this turn (its files stand and feed the first new steps):\n"
        f"{made}\n\nNot started (a revision replaces every one of these):\n" + "\n".join(waiting)
    )


async def _accepted(session: AsyncSession, task: Task, revision: PlanRevision) -> Plan:
    """The revision held to the graph rules and dressed as a first plan is: skills, bots, visual checks."""
    if not revision.plan.steps:
        return [], [], Criteria.model_validate(task.criteria or {})
    inputs = InputSet(items=[TaskInput.model_validate(i) for i in task.inputs])
    ask = await turn_ask(session, task)
    steps, edges, criteria = await turn_commit.accepted_lead_plan(session, task, revision.plan, ask, inputs)
    steps = gate_visual_steps(await pin_bots(pin_skills(steps, task), session, task))
    return steps, edges, criteria


async def _keep(
    session: AsyncSession, taken: Sequence[SteerNote], frontier: _Frontier, reason: str
) -> Replanned:
    """The Lead kept the plan: its steps not started carry the steers from now on, which are applied."""
    moved, task = await frontier_moved(session, frontier.task_id, frontier.turn_epoch, frontier.unstarted)
    if moved:
        return await _kept(session, frontier, moved=True)
    newest = max(n.epoch for n in taken)
    for step in await PlanStep.for_task(session, frontier.task_id):
        if step.id in frontier.unstarted and step.epoch < newest:
            step.epoch = newest  # taken: its contract carries them, and no later boundary asks again
    await steering.apply(session, task, None, taken, SafePoint.REPLAN)
    logfire.info("the Lead kept the plan of {t}: {why}", t=str(frontier.task_id), why=reason)
    return Replanned(moved=task.state is TaskStatus.PAUSED, said=f"{KEPT}: {reason}")


async def _kept(session: AsyncSession, frontier: _Frontier, *, moved: bool, why: str = "") -> Replanned:
    """The draft is dropped and nothing is written: release the lock, and say whether the frontier moved
    and, when it did not, `why` the plan stands."""
    task = await session.get(Task, frontier.task_id)
    moved = moved or (task is not None and task.state is TaskStatus.PAUSED)
    await session.rollback()
    return Replanned(moved=moved, said=f"{KEPT}: {why}" if why and not moved else KEPT)


async def _moved_after_error(session: AsyncSession, frontier: _Frontier) -> bool:
    """After an error: did the frontier really move, or the task pause? A check that cannot run reads as not."""
    try:
        moved, row = await frontier_moved(session, frontier.task_id, frontier.turn_epoch, frontier.unstarted)
        return moved or row.state is TaskStatus.PAUSED
    except Exception:
        logfire.exception("the Lead's re-plan of {t} could not read its frontier", t=str(frontier.task_id))
        return False
    finally:
        with contextlib.suppress(Exception):
            await session.rollback()
