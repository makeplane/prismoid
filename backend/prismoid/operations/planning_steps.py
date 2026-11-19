# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Planning's steps - what a drafted plan wears, how it is written, and how it reads back.

The pins (skills, bots), the device's `change` typing, the step rows and edges, and the one
`PlanStepView` constructor every surface builds from. Every planning path - the first draft, a
replan, the boundary's re-plan, the deterministic expansions - lands here, so the plan reads the
same whichever way it was made.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.agents.base import AgentDef, find, roster
from prismoid.ai.agents.bot_compile import BotCompileError, compile_bot
from prismoid.ai.rules.plan import DELIVERS_OUTCOME, Delivers, Makes
from prismoid.ai.skills import deliverables
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanEdge as PlanEdgeRow
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.errors import NotFound
from prismoid.operations import bots as bot_ops
from prismoid.operations import machines
from prismoid.operations import skills as skill_ops
from prismoid.schemas.bots import ResolvedBot
from prismoid.schemas.contract import BotPin, Criteria, SkillPin
from prismoid.schemas.device import DEVICE_DRIVER
from prismoid.schemas.events import TaskContinued
from prismoid.schemas.ids import StepId
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import (
    PlanEdge,
    PlanProposal,
    PlanStepView,
    PlanView,
    StepKind,
    StepSpec,
    StepState,
)


def _as_change(spec: StepSpec) -> StepSpec:
    """A coding step's contract, as the device delivers it: a `change` (its brief says where it goes).
    Anything else passes untouched."""
    contract = spec.contract
    if contract is None or contract.output is None or contract.output.type is not ArtifactType.CODE:
        return spec
    output = contract.output.model_copy(update={"type": ArtifactType.CHANGE})
    return spec.model_copy(update={"contract": contract.model_copy(update={"output": output})})


def typed_for_machine(steps: list[tuple[StepId, StepSpec]], task: Task) -> list[tuple[StepId, StepSpec]]:
    """On the device a coding step delivers a `change` (the work stays in the user's folder); decided
    from the task's driver alone, so the Lead never hears of it. Elsewhere the plan is unchanged."""
    if machines.driver_of(task) != DEVICE_DRIVER:
        return steps
    return [(step_id, _as_change(spec)) for step_id, spec in steps]


@dataclass(frozen=True, slots=True)
class TurnAsk:
    """A follow-up's own message and dials from its `task.continued` event; all None on turn 1 or with no
    event, so the row's stamp (the resolver's past answer) never reads as a fresh user choice."""

    message: str | None = None
    scale: str | None = None
    units: int | None = None
    deliverable: str | None = None
    format: str | None = None


async def turn_ask(session: AsyncSession, task: Task) -> TurnAsk:
    """This turn's own ask and explicit dials (see `TurnAsk`)."""
    if task.turn <= 1:
        return TurnAsk()
    latest = await TaskEvent.latest_of_type(session, task.id, "task.continued")
    if not isinstance(latest, TaskContinued):
        return TurnAsk()
    return TurnAsk(
        message=latest.message,
        scale=latest.scale,
        units=latest.target_pages,
        deliverable=latest.deliverable,
        format=latest.deliverable_format,
    )


async def write_steps(
    session: AsyncSession,
    task: Task,
    steps: list[tuple[StepId, StepSpec]],
    edges: list[PlanEdge],
    *,
    base_ordinal: int,
    epoch: int | None = None,
) -> list[PlanStep]:
    """Persist a plan's steps (topological order: a step with no undone upstream starts `ready`) and edges,
    for a draft or a replan (`base_ordinal` past the kept steps; `epoch` None stamps the current one)."""
    request = task.goal if (task.turn or 1) <= 1 else (await turn_ask(session, task)).message or ""
    steps = [
        (i, await _pin_format(session, s.model_copy(update={"request": request}), task))
        for i, s in typed_for_machine(steps, task)
    ]
    depended_on = {e.to_step for e in edges}

    rows: list[PlanStep] = []
    for i, (step_id, spec) in enumerate(steps):
        row = PlanStep(
            id=step_id,
            task_id=task.id,
            ordinal=base_ordinal + i,
            kind=spec.kind.value,
            state=(StepState.PENDING if step_id in depended_on else StepState.READY).value,
            title=spec.title,
            spec=spec.model_dump(mode="json"),
            epoch=task.plan_epoch if epoch is None else epoch,  # drafted under it, this turn
            turn_epoch=task.turn_epoch,
        )
        session.add(row)
        rows.append(row)

    # **The steps must exist before anything points at them.** `plan_edges.from_step_id` is a real
    # foreign key, and the unit of work does not reliably order these two inserts for us. An explicit
    # flush is cheaper than an implicit ordering rule that holds until it doesn't.
    await session.flush()
    for edge in edges:
        session.add(
            PlanEdgeRow(task_id=task.id, from_step_id=edge.from_step, to_step_id=edge.to_step, kind=edge.kind)
        )
    await session.flush()
    return rows


async def hand_downstream(
    session: AsyncSession, task_id: uuid.UUID, source: uuid.UUID, minted: Sequence[PlanStep]
) -> None:
    """An expansion's last step (`minted[-1]`, the synthesis or the assembly) takes over every edge from
    `source` to a step it did not mint: what waited on a recipe step waits on its whole shape."""
    ours = {row.id for row in minted}
    for edge in await PlanEdgeRow.for_task(session, task_id):
        if edge.from_step_id == source and edge.to_step_id not in ours:
            edge.from_step_id = minted[-1].id
    await session.flush()


async def _pin_format(session: AsyncSession, spec: StepSpec, task: Task) -> StepSpec:
    """A format maker's step with its format skill pinned (`skills.pin_format`); a bot never makes formats."""
    maker = find(str(spec.agent)) if spec.kind is StepKind.STEP else None
    if maker is None or not maker.makes_formats:
        return spec
    return await skill_ops.pin_format(session, spec, task.deliverable_format)


def kind_skill(kind: ArtifactType, doc_format: str | None) -> str | None:
    """The format skill a dialed kind pins (`formats`). A document pins one only for a file format: prose
    has real choices (report/summarize/email/...), and the Lead makes them per step."""
    if kind is ArtifactType.DOCUMENT:
        return deliverables.document_formats().get(doc_format or "")
    made = deliverables.format_for(kind)
    return made.skill if made is not None else None


async def dialed_pin(session: AsyncSession, task: Task) -> SkillPin | None:
    """The format skill a USER-dialed deliverable pins, at its head now; None for an undialed or prose kind,
    or a skill with no published head (fail-soft: a person waits on the turn, and the loss costs the pin)."""
    dialed = task.user_deliverable
    if not dialed:
        return None
    name = kind_skill(ArtifactType(dialed), task.deliverable_format)
    if name is None:
        return None
    try:
        return await skill_ops.resolve_ref(session, name)
    except NotFound:
        logfire.warn("dialed skill {name} unavailable for {task_id}; no pin", name=name, task_id=str(task.id))
        return None


async def step_pins(session: AsyncSession, proposal: PlanProposal) -> dict[str, SkillPin]:
    """Each skill a plan's steps name, at its published head now: the version every attempt of the step loads.
    A name with no head left (unpublished since the plan's check) is left out, fail-soft."""
    pins: dict[str, SkillPin] = {}
    for name in sorted({skill for step in proposal.steps for skill in step.skills}):
        try:
            pins[name] = await skill_ops.resolve_ref(session, name)
        except NotFound:
            logfire.warn("planned skill {name} has no published head; the step runs without it", name=name)
    return pins


def pin_skills(steps: list[tuple[StepId, StepSpec]], task: Task) -> list[tuple[StepId, StepSpec]]:
    """The task's requested skills (resolved at submit) onto every `STEP`-kind step, before the step's own;
    a copy, not a lookup, so the pins survive a replan unchanged."""
    pins = [SkillPin.model_validate(p) for p in (task.skill_pins or [])]
    if not pins:
        return steps
    names = {pin.name for pin in pins}
    return [
        (
            sid,
            spec.model_copy(
                update={"skill_pins": [*pins, *(p for p in spec.skill_pins if p.name not in names)]}
            )
            if spec.kind is StepKind.STEP
            else spec,
        )
        for sid, spec in steps
    ]


async def bot_roster(session: AsyncSession, task: Task) -> list[AgentDef]:
    """This task's bots, compiled into agents One may put in a step.

    Auto-included from the task's Project and its creator's own published bots, unless the task
    named some explicitly (`tasks.bot_pins`) - a roster you must remember to attach is not a
    roster, and an override is how you pin a draft or narrow a crowded space.

    **A bot that cannot compile is dropped, loudly, and the plan goes on.** A stale row written
    before a rule tightened must not be able to take down planning for a task that never asked
    for it: the loss costs that bot's availability, never the task. Same posture the fleet takes
    when a seat dies, and the media lane when a chain does.
    """
    defs: list[AgentDef] = []
    for bot in await resolved_bots(session, task):
        try:
            defs.append(compile_bot(bot))
        except BotCompileError as bad:
            logfire.warn("bot {ref} excluded from the roster: {why}", ref=bot.ref(), why=str(bad))
    return defs


async def resolved_bots(session: AsyncSession, task: Task) -> list[ResolvedBot]:
    """This task's bots at the versions it plans from; none when the roster cannot be read (fail-soft: a
    person waits on the turn, and the loss costs the bots, never the task). Commits nothing."""
    creator = await session.get(Actor, task.creator_actor_id)
    if creator is None:
        return []
    pins = [BotPin.model_validate(p) for p in (task.bot_pins or [])]
    try:
        return await bot_ops.roster_for_task(session, creator, project_id=task.project_id, pins=pins)
    except Exception:
        logfire.exception("bot roster unavailable for {task_id}; planning without it", task_id=str(task.id))
        return []


async def pin_bots(
    steps: list[tuple[StepId, StepSpec]], session: AsyncSession, task: Task
) -> list[tuple[StepId, StepSpec]]:
    """Stamp each bot step with the exact version that will run it.

    `spec.agent` names the role. Without a pin the executor would resolve that name to the bot's
    *head* at run time, so publishing v2 mid-task would silently change what the task is doing -
    the one thing pins exist to prevent, and the reason `skill_pins` exists one level down.

    Built-in steps are left alone (they have no versions), and a bot that cannot be resolved
    leaves its step unpinned rather than failing the plan: the executor will then refuse that one
    step honestly instead of the whole task dying at planning time for a row someone archived.
    """
    builtin = {str(a.name) for a in roster()}
    wanted = {
        str(spec.agent)
        for _, spec in steps
        if spec.kind is StepKind.STEP and spec.agent and str(spec.agent) not in builtin
    }
    if not wanted:
        return steps

    creator = await session.get(Actor, task.creator_actor_id)
    explicit = {str(p.get("name")): BotPin.model_validate(p) for p in (task.bot_pins or [])}
    pins: dict[str, BotPin] = {}
    for name in sorted(wanted):
        pin = explicit.get(name)  # an explicit attach may pin a draft; the head never would
        if pin is None and creator is not None:
            try:
                pin = await bot_ops.resolve_ref(session, name, actor=creator, project_id=task.project_id)
            except Exception:
                logfire.warn("bot {name} planned but unresolvable; step left unpinned", name=name)
        if pin is not None:
            pins[name] = pin

    return [
        (
            sid,
            spec.model_copy(update={"bot_pins": [pins[str(spec.agent)]]})
            if spec.agent and str(spec.agent) in pins
            else spec,
        )
        for sid, spec in steps
    ]


def plan_kind(a: AgentDef) -> Makes | Delivers | None:
    """What a step of this agent delivers, flattened for `rules.plan.validate`, which never reaches the
    registry: the kinds of file it publishes, its outcome (the operator), or nothing."""
    if a.delivers_outcome:
        return DELIVERS_OUTCOME
    return a.kinds if a.publishes else None


def step_view(r: PlanStep) -> PlanStepView:
    """The ONE PlanStepView constructor - `view_of` (the task.planned payload) and
    `plan_view` (the REST plan) both build from here, so they can never drift again
    (they already had: one carried `attempt`, the other didn't). Carries the step's
    contract acceptance and the fleet seat's model_ref as data (the progress tree)."""
    spec = r.spec or {}
    contract = spec.get("contract") or {}
    return PlanStepView(
        id=r.id,  # type: ignore[arg-type]
        title=r.title,
        kind=r.kind,  # type: ignore[arg-type]
        state=r.step_state,
        agent=spec.get("agent"),
        attempt=r.attempt,
        acceptance=list(contract.get("acceptance") or []),
        model_ref=spec.get("model_ref"),
        lane_role=spec.get("lane_role"),  # the long-form grouping key - the tree reads it
        why=str(spec.get("why", "")),  # One's reason for this agent
        bot_version=next(
            (p.get("version") for p in (spec.get("bot_pins") or []) if p.get("version")), None
        ),  # the plan-time pin, made legible
    )


def view_of(rows: list[PlanStep], criteria: Criteria) -> PlanView:
    return PlanView(steps=[step_view(r) for r in rows], criteria=criteria)


async def plan_view(session: AsyncSession, task: Task) -> PlanView:
    """The plan, as the API and the CLI see it - **steps, edges AND criteria**.

    It used to return the steps and silently drop the other two, which nothing noticed because
    nothing looked: `GET /v1/tasks/{id}` was handing back a hard-coded empty `PlanView()` with a
    comment promising the real one. So the plan was invisible on every surface, the criteria
    the work is *held to* were invisible with it, and `prismoid tasks show` reported `steps: 0` for a
    task that had steps. A contract nothing checks is a hope with a schema (the lesson
    recurs). The check *plan visible in `tasks show`* is what finally looked.
    """
    rows = await PlanStep.for_task(session, task.id)
    edges = await PlanEdgeRow.for_task(session, task.id)
    return PlanView(
        steps=[step_view(r) for r in rows],
        edges=[
            PlanEdge(
                from_step=e.from_step_id,  # type: ignore[arg-type]
                to_step=e.to_step_id,  # type: ignore[arg-type]
                kind=e.kind,  # type: ignore[arg-type]
            )
            for e in edges
        ],
        criteria=Criteria.model_validate(task.criteria or {}),
    )
