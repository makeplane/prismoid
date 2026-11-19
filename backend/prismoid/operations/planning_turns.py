# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Planning's turn helpers - the parts of a planning pass that ask no model.

`planning.py` drafts and commits a turn's plan and `turn/` runs the Lead around it; this module holds
what they share: the fleet recipe, the Lead's plan record, a proposal's acceptance and its floor, the
dialed kind's skill pin, and the writes a re-plan makes. Each helper works in the caller's transaction and
commits nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.agents.base import AgentDef
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.resolve import chain_for, seatable
from prismoid.ai.gateway.run_cost import RUN_SIZES, priced
from prismoid.ai.rules.plan import MAX_STEPS, Plan, validate
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanEdge as PlanEdgeRow
from prismoid.db.models.plan import PlanEdit as PlanEditRow
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.operations import connectors as connector_ops
from prismoid.operations import machines, steering
from prismoid.operations import models as model_ops
from prismoid.operations import usage as usage_ops
from prismoid.operations.attribution import actor_ref
from prismoid.operations.gate_budget import budget_of
from prismoid.operations.plan_shapes import fleet_plan, one_step_plan
from prismoid.operations.planning_steps import dialed_pin, plan_kind, step_pins, write_steps
from prismoid.operations.turn.recipes import Lane, lane_of
from prismoid.schemas import sizing
from prismoid.schemas.computer_use import named_sites
from prismoid.schemas.context import ContextPack
from prismoid.schemas.contract import Criteria
from prismoid.schemas.events import PlanEdited
from prismoid.schemas.ids import ProjectId, StepId, TaskId
from prismoid.schemas.inputs import InputSet
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import (
    PlanEdge,
    PlanEditKind,
    PlanEstimate,
    PlanProposal,
    StepSpec,
    StepState,
    TaskMode,
)
from prismoid.schemas.plan import PlanEdit as PlanEditView
from prismoid.schemas.sizing import SizeBucket
from prismoid.schemas.steering import SteerNote, replan_reason

UNSTARTED: Final = (StepState.PENDING, StepState.READY)
IN_FLIGHT: Final = (StepState.RUNNING, StepState.VERIFYING, StepState.RETRYING)
_MANY_HANDS: Final = (TaskMode.FLEET.value, TaskMode.COUNCIL.value)  # the second: pre-rename rows


@dataclass(slots=True)
class Routed:
    """One planning pass's answer, before it is written: the steps, the lane, the reason."""

    steps: list[tuple[StepId, StepSpec]]
    edges: list[PlanEdge]
    criteria: Criteria
    lane: str
    lane_source: str
    lane_why: str
    pack: ContextPack | None
    fallback: str = ""  # the Lead's plan failed and its one-step floor runs: why

    @classmethod
    def of(cls, lane: Lane, pack: ContextPack | None) -> Routed:
        """A rung's lane, as the pass's answer."""
        return cls(
            steps=lane.steps,
            edges=lane.edges,
            criteria=lane.criteria,
            lane=lane.name,
            lane_source=lane.source,
            lane_why=lane.why,
            pack=pack,
            fallback=lane.fallback,
        )


# ── the deterministic rungs ───────────────────────────────────────────────────


async def fleet_rung(session: AsyncSession, task: Task, message: str, inputs: InputSet) -> Lane | None:
    """Fleet mode: one pinned answer per eligible enabled model the budget left can buy, then the writer's
    table of them. Stamps the row's size first (see below)."""
    if task.mode not in _MANY_HANDS:
        return None
    # THE FLEET SIZE STAMP: fleet answers are single-breath - the door refuses an explicit big
    # bucket, and a count that RESOLVES big is clamped to standard HERE with the row stamped, so
    # the read-back stays honest. This is the planning-time half the door's docstring promises.
    bucket, units = sizing.resolve(task.scale, None, task.target_pages)
    if not sizing.fleet_allows(bucket):
        bucket, units = SizeBucket.STANDARD, None
    task.scale, task.target_pages = bucket.value, units
    # Eligibility is data, read from the catalog here so the plan rules stay pure: any enabled row
    # serving an executor class that this host can build gets a seat (a judge- or vision-only row
    # cannot author a document; a keyless row cannot answer). MAX_STEPS caps the width, minus the
    # table's step. The flag was checked at submit; an empty catalog falls back to the one-step plan.
    catalog, order = await model_ops.routed_catalog(session)
    eligible = seatable(catalog)
    if task.model_refs:
        # The creator chose seats (validated at submit). Intersect with live eligibility -
        # a ref disabled since submit costs the seat; losing every seat falls back to the
        # one-step plan below, the same as an empty catalog.
        chosen = set(task.model_refs)
        eligible = [m for m in eligible if m.ref in chosen]
    if not eligible:
        plan = one_step_plan(message, inputs.items)
        return lane_of(plan, "fleet", "dial", "no model can hold a seat; one step answers")
    head = chain_for(ModelClass.FAST_EXECUTOR, catalog, order=order.get(ModelClass.FAST_EXECUTOR))
    left = await _budget_left(session, task)
    seats, _ = fleet_seats(eligible[: MAX_STEPS - 1], head[0] if head else None, left)
    return lane_of(
        fleet_plan(message, inputs.items, models=seats),
        "fleet",
        "dial",
        "every eligible model answers; the writer lays them in one table",
    )


TABLE_LABEL: Final = "table"


def fleet_seats(
    eligible: Sequence[ModelSpec], table: ModelSpec | None, budget_left: Decimal | None
) -> tuple[list[str], PlanEstimate]:
    """The refs a fleet seats, in catalog order: each whose writer run at its own row's prices still fits the
    budget left beside the table and the seats before it (the first always sits), and their estimate."""
    size = RUN_SIZES["writer"]
    costs = [priced(TABLE_LABEL, ModelClass.FAST_EXECUTOR, size, table.prices if table else {})]
    seats: list[str] = []
    for row in eligible:
        cost = priced(row.ref, ModelClass.FAST_EXECUTOR, size, row.prices)
        if seats and budget_left is not None and sum(c.usd for c in costs) + cost.usd > budget_left:
            continue
        seats.append(row.ref)
        costs.append(cost)
    return seats, PlanEstimate.of(tuple(costs), budget_left)


async def _budget_left(session: AsyncSession, task: Task) -> Decimal | None:
    """What the task's budget has left to spend, or None for a task without one."""
    limit = budget_of(task.budget)
    if limit is None:
        return None
    return max(limit - (await usage_ops.for_task(session, task.id)).total_usd, Decimal(0))


async def consensus_architects(session: AsyncSession, task: Task) -> list[tuple[str, str]]:
    """The consensus outline (quality=max): two or three architects from distinct families, merged; fewer
    than two families degrades to the single architect. Empty below quality=max."""
    if task.quality != "max":
        return []
    catalog, order = await model_ops.routed_catalog(session)
    chain = chain_for(ModelClass.FRONTIER_REASONER, catalog, order=order.get(ModelClass.FRONTIER_REASONER))
    by_family: dict[str, str] = {}
    for m in chain:  # chain order = routing order; first per family is its best
        by_family.setdefault(str(m.family), m.ref)
    return list(by_family.items())[:3]


async def record_lead(session: AsyncSession, task: Task, say: str, lead_step: PlanStep) -> None:
    """Journal a Lead's plan: the `plan_edits` row and its `plan.edited{lead}` event, in the caller's
    transaction (claims a seq, so the task row is held to its commit)."""
    now = utcnow()
    payload = {"reason": say, "step_id": str(lead_step.id)}
    session.add(
        PlanEditRow(task_id=task.id, kind=PlanEditKind.LEAD.value, reason=say[:2000], payload=payload)
    )
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        PlanEdited(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=now,
            edit=PlanEditView(kind=PlanEditKind.LEAD, reason=say[:200], at=now),
        ),
    )


# ── the tail every lane shares ────────────────────────────────────────────────


def final_kind(steps: list[tuple[StepId, StepSpec]]) -> ArtifactType | None:
    """The kind the plan DELIVERS: the last publishing step's typed output, the step the plan rules hold to
    a chosen deliverable (steps arrive in topological order, so the last typed output is the deliverable)."""
    for _, spec in reversed(steps):
        if spec.contract is not None and spec.contract.output is not None:
            return spec.contract.output.type
    return None


def stamp_plan_kind(task: Task, lane: str, steps: list[tuple[StepId, StepSpec]]) -> None:
    """THE PLAN-STAMP: when neither dial nor language resolved the path, the
    validated plan's own final publishing step is the answer - stamped as "plan" so the
    chip can say what the task became. Same commit as the steps; replans re-stamp
    idempotently (same inputs, same law). A carried non-user stamp the new plan disagrees
    with is re-stamped too - the row says what THIS turn became. Talk and direct
    turns leave the stamp alone (a reply is not a kind; the DM lane never stamped)."""
    if lane in ("express", "direct") or task.mode in _MANY_HANDS:
        return
    plan_kind_now = final_kind(steps)
    if (
        plan_kind_now is not None
        and task.user_deliverable is None
        and plan_kind_now.value != task.deliverable
    ):
        task.deliverable, task.deliverable_source = plan_kind_now.value, "plan"


async def pin_dialed_skill(session: AsyncSession, task: Task, lane: str) -> None:
    """A USER-dialed deliverable's format skill, pinned on the task when the person pinned nothing
    (never on the express or direct lane). Commits nothing."""
    if task.skill_pins or task.mode in _MANY_HANDS or lane in ("express", "direct"):
        return
    if (pin := await dialed_pin(session, task)) is not None:
        task.skill_pins = [pin.model_dump(mode="json")]


# ── a proposal ─────────────────────────────────────────────────────────────────


async def accept_proposal(
    session: AsyncSession,
    task: Task,
    proposal: PlanProposal,
    *,
    plannable: Sequence[AgentDef],
    inputs: InputSet,
    deliverable: ArtifactType | None,
    ask: str | None,
    recipes: Mapping[str, Plan] | None = None,
    size: tuple[SizeBucket, int | None] | None = None,
) -> Plan:
    """Hold a proposed plan to the plan rules, each step in `recipes` expanded into its code-made shape,
    each skill a step names pinned at its head and `size` (the turn's size dial) banding its typed steps;
    raises `InvalidPlan` naming the rule it broke."""
    # name → what it publishes: the rules type each publishing step's contract from this without
    # ever importing the registry.
    plan = validate(
        proposal,
        roster={a.name: plan_kind(a) for a in plannable},
        browses=[a.name for a in plannable if a.browses and settings.computer_use.enabled],
        inputs=inputs.items,
        deliverable=deliverable,
        recipes=recipes,
        pins=await step_pins(session, proposal),
        size=size,
    )
    logfire.info("planned {task_id}: {n} step(s)", task_id=str(task.id), n=len(plan[0]))
    return await seed_named_hosts(session, task, plan, f"{ask or ''}\n{task.goal}")


async def seed_named_hosts(session: AsyncSession, task: Task, plan: Plan, words: str) -> Plan:
    """A plan's browser steps may open the hosts the person typed without asking (a proposal carries no
    domains); only their own words count, and a connector-covered host is left to its connector."""
    hosts = named_sites(words)
    if not hosts:
        return plan
    covered = await connector_ops.connector_hosts(session, task)
    hosts = tuple(h for h in hosts if not connector_ops.covered_by(covered, h))
    steps, edges, criteria = plan
    seeded = [
        (
            step_id,
            spec.model_copy(
                update={
                    "contract": spec.contract.model_copy(
                        update={"allow_domains": sorted({*spec.contract.allow_domains, *hosts})}
                    )
                }
            ),
        )
        if hosts and spec.contract is not None and machines.seat_profile(task, spec) == "browser"
        else (step_id, spec)
        for step_id, spec in steps
    ]
    return seeded, edges, criteria


# ── re-plans ──────────────────────────────────────────────────────────────────


async def frontier_moved(
    session: AsyncSession, task_id: uuid.UUID, turn_epoch: int, unstarted_ids: Sequence[uuid.UUID]
) -> tuple[bool, Task]:
    """Under the task row lock (taken here; every steer and plan writer takes it first): did the
    frontier a draft was for move - a step started (or is waiting out a pace), an editor's replan
    rewrote the remainder, the turn ended or moved on? Read by id, so an instance a rollback expired is never touched; the row
    comes back fresh (the session's own instance, refreshed) for the caller's own questions."""
    task = await Task.held(session, task_id)
    states = await PlanStep.states(session, task_id)
    moved = (
        task.is_terminal()
        or task.turn_epoch != turn_epoch
        or any(state in IN_FLIGHT for state in states.values())
        or {sid for sid, state in states.items() if state in UNSTARTED} != set(unstarted_ids)
        or await PlanStep.any_pacing(session, task_id)
    )
    return moved, task


@dataclass(frozen=True, slots=True)
class DoneWork:
    """This turn's done steps, as a re-plan reads them: the lines the Lead is told,
    and the steps with live files that feed every new root."""

    steps: list[PlanStep]
    lines: list[str]
    feeding: list[uuid.UUID]


async def done_work(session: AsyncSession, steps_now: Sequence[PlanStep], turn_epoch: int) -> DoneWork:
    """What this turn already made (see `DoneWork`)."""
    done = [s for s in steps_now if s.step_state is StepState.DONE and s.turn_epoch == turn_epoch]
    lines: list[str] = []
    feeding: list[uuid.UUID] = []
    for step in done:
        files = [a.title for a in await Artifact.for_step(session, step.id) if a.role != "superseded"]
        lines.append(f"- {step.title}: {', '.join(files)}")
        if files:
            feeding.append(step.id)
    return DoneWork(steps=done, lines=lines, feeding=feeding)


async def replace_unstarted(
    session: AsyncSession,
    task: Task,
    plan: Plan,
    *,
    taken: Sequence[SteerNote],
    steps_now: Sequence[PlanStep],
    unstarted_ids: Sequence[uuid.UUID],
    feeding: Sequence[uuid.UUID],
    drafted: int,
    actor: Actor | None = None,
) -> None:
    """A re-plan's writes under the caller's task row lock: drop the unstarted steps, write the draft after
    the done ones, wire done work to its roots, journal the edit (`actor` None for a steer's re-plan)."""
    task_id = task.id
    steps, edges, criteria = plan
    await PlanStep.delete_unfinished(session, task_id)
    kept = [s for s in steps_now if s.step_state is StepState.DONE]
    await write_steps(
        session,
        task,
        steps,
        edges,
        base_ordinal=max((s.ordinal for s in kept), default=-1) + 1,
        epoch=drafted,
    )
    # Every done step of this turn with live files feeds every new root (upstream mounts follow
    # edges): what the turn already made reaches what remains, a skip edge included.
    # A root over done work is ready at once.
    fed = {e.to_step for e in edges}
    for source in feeding:
        for root in (sid for sid, _ in steps if sid not in fed):
            session.add(PlanEdgeRow(task_id=task_id, from_step_id=source, to_step_id=root, kind="depends_on"))
    task.criteria = criteria.model_dump(mode="json")

    reason = replan_reason(taken)
    now = utcnow()
    session.add(
        PlanEditRow(
            task_id=task_id,
            kind=PlanEditKind.REPLAN.value,
            reason=reason,
            payload={"reason": reason},
            actor_id=actor.id if actor else None,
        )
    )
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        PlanEdited(
            seq=seq,
            task_id=TaskId(task_id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=now,
            actor=actor_ref(actor) if actor else None,
            edit=PlanEditView(
                kind=PlanEditKind.REPLAN,
                reason=reason,
                at=now,
                added=[sid for sid, _ in steps],
                removed=[StepId(sid) for sid in unstarted_ids],
            ),
        ),
    )
    await session.flush()
    await steering.apply_planned(session, task, drafted)
