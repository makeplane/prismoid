# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The attempt's second stage: load the step's frame and build the run's hands. The sources, the
pinned skills and the connectors go in before the agent exists; then `TaskDeps` gathers every
port the run reaches the world through (the message lane, the taps, the browser, the inbox).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.agents.base import definition_for
from prismoid.ai.deps import TaskDeps
from prismoid.ai.frames.driver import FrameDriver
from prismoid.ai.tools.call_rows import JournalTool
from prismoid.ai.tools.catalog import model_name, taint_of
from prismoid.db.models.actor import Actor
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanEdge, PlanStep
from prismoid.db.models.task import Task
from prismoid.operations import bots as bot_ops
from prismoid.operations import connectors as connector_ops
from prismoid.operations import live, machines
from prismoid.operations import longform as longform_ops
from prismoid.operations import routines as routine_ops
from prismoid.operations import skills as skill_ops
from prismoid.operations import sources as sources_ops
from prismoid.operations import usage as usage_ops
from prismoid.operations.execution import ports
from prismoid.operations.execution.inbox import StepInbox
from prismoid.operations.execution.mounts import (
    materialise_inputs,
    materialise_skills,
    prior_figures,
    prior_sections,
    superseded_output,
)
from prismoid.operations.execution.taps import preview, tool_journal
from prismoid.operations.standing import creator_standing
from prismoid.schemas.bots import ResolvedBot
from prismoid.schemas.connectors import ConnectorSpec
from prismoid.schemas.egress import Taint, joined
from prismoid.schemas.ids import ProjectId, StepId, TaskId
from prismoid.schemas.park import park_of
from prismoid.schemas.plan import StepSpec, TaskMode
from prismoid.schemas.sizing import web_effort_for
from prismoid.schemas.standing import Standing


@dataclass(frozen=True)
class Bench:
    """The frame the step holds and what it was granted."""

    frame: FrameDriver
    acquired: machines.Acquired
    allowed: set[str]
    profile: str
    seat: bool


@dataclass(frozen=True)
class Equipped:
    """What the run is handed: its ports, the inputs on disk, the bots it may be served by, the
    inbox whose `told` the park saves, and whether a later citing step reads its `[S#]` marks."""

    deps: TaskDeps
    inputs: tuple[str, ...]
    bots: tuple[ResolvedBot, ...]
    inbox: StepInbox
    cited_later: bool = False


async def equip(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    spec: StepSpec,
    *,
    bench: Bench,
    upstream: Sequence[Artifact],
    resume: dict[str, object] | None,
    wire: live.Wire,
    fact: live.ActivityFact,
) -> Equipped:
    """Mount the step's inputs, skills and connectors, then build its `TaskDeps`."""
    inputs = await _mount_inputs(session, task, step, spec, frame=bench.frame, upstream=upstream, fact=fact)
    if spec.lane_role == "assembly":  # a long document's parts, their citations rendered under one numbering
        inputs = await longform_ops.render_parts_mounted(
            bench.frame, spec, inputs, await sources_ops.source_ids(task.id)
        )
    # The pinned skills, into `/skills/<name>/SKILL.md`, right beside the sources and before the
    # agent exists. Re-run on resume for the same reason inputs are: `/skills` is excluded from
    # the snapshot (it is content-addressed and re-fetchable), so a resumed frame has an empty
    # `/skills` until this puts the bodies back.
    await materialise_skills(session, spec, bench.frame)
    for pin in spec.skill_pins:
        await fact("end", f"Loaded skill {pin.name}")

    journal = tool_journal(session, task, step)
    # The bot this step runs on, at the version the plan pinned. Resolved HERE and
    # nowhere else - the same law `connectors` follows - so the light agents (planner, judge,
    # router, clarifier, namer, memory_extract) can never be served by a user's bot:
    # not a check, but the fact that the value never reaches the call that would build one.
    step_bots = await bots_for(session, task, spec)
    cites = definition_for(str(spec.agent), TaskDeps(bots=step_bots)).cites
    connectors = await mount_connectors(session, task, step_bots, journal=journal, fact=fact)

    # The safe points' inbox, on its own short sessions; a resume restores how far
    # the parked run was told. A pause parks this run only while the deployment's parked pauses
    # are under the cap - read when the pause is seen (`StepInbox.pending`), never here: each
    # park holds a queue slot, so at the cap the step finishes and the frontier holds the pause.
    inbox = StepInbox(task.id, step.id, task.turn, told=told_of(resume, step))
    deps = TaskDeps(
        task_id=TaskId(task.id),
        step_id=StepId(step.id),
        project_id=ProjectId(task.project_id) if task.project_id else None,
        frame=bench.frame,
        # The message lane: the step agent's visible text streams as message.* frames
        # through this wire. Only here - judges, planner, and verification runs never get emit_event,
        # so nothing they say can appear in a user's thread.
        emit_event=live.emitter(wire),
        emit_usage=usage_ops.emitter(session, own_transaction=True),
        journal_tool=journal,
        preview_tool=preview(session, task, step, wire),
        keep_page=sources_ops.keeper(task.id),  # every page `web.fetch` reads, kept for the receipt
        sources=await sources_ops.source_ids(task.id) if cites else {},  # what earlier steps read
        inputs=inputs,
        skill_pins=list(spec.skill_pins),  # so publish provenance can stamp what shaped the work
        lane_role=spec.lane_role,  # a lane step's agent runs in that part's mode (a scatter seat)
        connectors=connectors,  # the mounted MCP servers - build() turns them into toolsets
        bots=step_bots,  # `build()` compiles one of these into the AgentDef it runs
        # `routine.create`'s hands - a closure on its OWN session (this one sits
        # committed-and-idle across the run). The routine lands born-PAUSED under the task's
        # creator; only granted agents ever see the tool, but the wire is always live.
        propose_routine=routine_ops.proposer(task.id, task.creator_actor_id, task.project_id),
        # The browser family's hands: live only on a browser-profiled frame, so
        # a task-lane step whose agent never named that profile gets no toolset at all
        # (`build()`'s `deps.browser is not None` check) - absent, not merely empty.
        browser=(
            ports.browser_ports(bench.frame, bench.acquired, bench.allowed, task, step)
            if bench.profile == "browser"
            else None
        ),
        steering=inbox,
        # The run guard's eyes: the task's spend against its cap, on
        # its own short sessions. Every step agent reads it: a guarded one for its brakes, any
        # other for the cap's hold alone (`RunGuard.budget_only`).
        spend=usage_ops.spend_reader(task.id),
        # A grant revoked while this step runs.
        regrant=machines.grant_fence(task.id, bench.frame, seat=bench.seat),
        # Research as deep as the deliverable: a memo reads a few sources, a report the caps.
        web_effort=web_effort_for(task.scale),
        unattended=task.origin_routine_id is not None and task.turn == 1,  # a routine's run (ADR-054)
        taint_floor=await taint_floor(session, task, step),
    )
    later = cites and await cited_later(session, task, step)
    return Equipped(deps=deps, inputs=inputs, bots=step_bots, inbox=inbox, cited_later=later)


async def taint_floor(session: AsyncSession, task: Task, step: PlanStep) -> Taint:
    """Whose text the task's other steps read, which their outputs, the plan and the thread carry into
    this step: a page, a search or a connected service makes its connector writes ask from the start."""
    tools = await TaskEvent.ok_tools_besides(session, task_id=task.id, step_id=step.id)
    return joined(*(taint_of(model_name(tool)) for tool in tools))


async def cited_later(session: AsyncSession, task: Task, step: PlanStep) -> bool:
    """Does a step that reads this one's output, at any remove, cite? Then it renders the `[S#]` marks."""
    edges = await PlanEdge.for_task(session, task.id)
    agents = {s.id: str(s.spec.get("agent") or "") for s in await PlanStep.for_task(session, task.id)}
    seen: set[uuid.UUID] = set()
    reached = [step.id]
    while reached:
        at = reached.pop()
        for later in (e.to_step_id for e in edges if e.from_step_id == at and e.to_step_id not in seen):
            seen.add(later)
            reached.append(later)
    return any(_cites(agents.get(later, "")) for later in seen)


def _cites(agent: str) -> bool:
    try:
        return definition_for(agent, TaskDeps()).cites
    except KeyError:  # a bot's step: bots do not cite
        return False


async def _mount_inputs(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    spec: StepSpec,
    *,
    frame: FrameDriver,
    upstream: Sequence[Artifact],
    fact: live.ActivityFact,
) -> tuple[str, ...]:
    """The sources, into the sandbox, **before the agent exists**. Read-only, root-owned: an
    agent that can rewrite its own sources can launder a hallucination into a verified fact (the
    frame image enforces the other half - see its Dockerfile). Re-run on resume too: the same
    bytes to the same read-only paths is idempotent, and it keeps `deps.inputs` - what `publish`
    stamps as sources - identical across the pause without threading it through the persisted
    state."""
    mount_started = time.monotonic()
    # Seat names are a FLEET phenomenon: same-goal answers from DIFFERENT models need
    # per-model filenames or they mount over each other. Keying this off
    # `tolerates_missing_inputs` was the chair-era shorthand - and it inverted the disease
    # the moment a SECOND tolerant consumer existed: the long-form assembly had
    # its nine same-model sections + outline collapsed onto ONE `answer-<model>.md`, nine
    # overwrites deep.
    # Sections keep their own `NN <title>` names; only a fleet's tolerant consumer renames.
    fleet_consumer = (
        TaskMode(task.mode) in (TaskMode.FLEET, TaskMode.COUNCIL) and spec.tolerates_missing_inputs
    )
    named = spec.contract.inputs if spec.contract else ()
    inputs = await materialise_inputs(
        session,
        task,
        frame,
        upstream,
        seat_names=fleet_consumer,
        superseded=await superseded_output(session, task, step),
        # A long-form follow-up's outline, parts and assembly: the last document's section
        # text and figures their contract names (any other step's names none, at no query).
        carried=[
            *await prior_sections(session, task, named=named),
            *await prior_figures(session, task, named=named),
        ],
    )
    if inputs:
        names = ", ".join(p.rsplit("/", 1)[-1] for p in inputs[:5])
        await fact(
            "end",
            f"Mounted {len(inputs)} input{'' if len(inputs) == 1 else 's'} - {names}",
            started=mount_started,
        )
    return inputs


# Why a creator who cannot act mounts nothing, as each dropped connector's journal line says it.
_GONE: dict[Standing, str] = {
    Standing.SUSPENDED: "the task's creator is suspended, and a connector is their credential",
    Standing.LEFT: "the task's creator left this workspace, and a connector is their credential",
}


async def mount_connectors(
    session: AsyncSession,
    task: Task,
    step_bots: tuple[ResolvedBot, ...],
    *,
    journal: JournalTool,
    fact: live.ActivityFact,
) -> tuple[ConnectorSpec, ...]:
    """The connectors: refresh, probe, decrypt - every drop journaled as a tool.called
    ok=false naming the fix (it costs the capability, never the task), every mount narrated like
    inputs and skills are. `resolve_for_task` dials out only on a committed, idle session; the
    commit before the run still follows. A creator who is suspended or out of the workspace mounts
    NOTHING, task-level or a bot's declared ones alike, each a journaled drop naming why: the
    credential is theirs, whoever's turn it is (ADR-087 section 2)."""
    bot_slugs = [slug for bot in step_bots for slug in bot.manifest.connectors]
    if not task.connector_ids and not bot_slugs:
        return ()  # nothing to mount, so nobody's standing to read
    standing = await creator_standing(session, task)
    if standing is not Standing.ACTIVE:
        dropped = await connector_ops.drop_all(
            session, task, bot_slugs, journal=journal, reason=_GONE[standing]
        )
        for note in dropped.notes:
            await fact("end", note)
        return ()
    resolved = await connector_ops.resolve_for_task(session, task, journal=journal, bots=step_bots)
    for note in resolved.notes:
        await fact("end", note)

    # The bot's DECLARED connectors, mounted at last:
    # the step's bots' manifest slugs resolve against the CREATOR's own rows - credentials
    # never travel with a bot - deduped against the task-level attaches above, extending
    # this step's specs only (the populated-only-here fence holds; task.connector_ids is
    # never written). A slug the creator has not connected is a journaled drop, never a
    # task failure; every mount gates writes on approval (nobody clicked Connect here).
    if bot_slugs:
        bot_mounts = await connector_ops.resolve_for_bot(
            session,
            task,
            bot_slugs,
            already=[s.slug for s in resolved.specs],
            journal=journal,
            bots=step_bots,
        )
        for note in bot_mounts.notes:
            await fact("end", note)
        resolved.specs += bot_mounts.specs
    return resolved.specs


def told_of(resume: dict[str, object] | None, step: PlanStep) -> int:
    """How far a run has been told: a park saved it and its resume restores it; a fresh
    run starts at the step's epoch, whose steers its contract already carries."""
    return step.epoch if resume is None else park_of(resume).told_epoch


async def settle_skill_pins(session: AsyncSession, task: Task, step: PlanStep, spec: StepSpec) -> StepSpec:
    """`spec` with only the pinned skills its agent can follow (`skills.honoured`), before anything mounts one. The
    plan pins a format maker's skill; a step stored without one is pinned once here and the pin written back."""
    definition = definition_for(str(spec.agent), TaskDeps(bots=await bots_for(session, task, spec)))
    pinned = (
        await skill_ops.pin_format(session, spec, task.deliverable_format)
        if definition.makes_formats
        else spec
    )
    if pinned != spec:
        spec = pinned
        step.spec = {**step.spec, "skill_pins": [p.model_dump(mode="json") for p in pinned.skill_pins]}
        await session.flush()
    if not spec.skill_pins:
        return spec
    return await skill_ops.honoured(session, spec, definition.tools, tool_journal(session, task, step))


async def bots_for(session: AsyncSession, task: Task, spec: StepSpec) -> tuple[ResolvedBot, ...]:
    """The bots this ONE step may be served by - normally exactly one, or none for a built-in.

    Narrow on purpose. `TaskDeps.bots` is the roster `build()` resolves a name against, and a step
    only ever needs the agent it was planned onto; handing it the whole workspace roster would let
    a retry silently land on a different bot if the plan were ever edited underneath it.

    A pin that no longer resolves (archived, deleted, or a scope that changed) comes back empty,
    and `build()` then fails this step with `no agent named …` - an honest step failure naming the
    missing agent, rather than a task that quietly runs on somebody else's bot.
    """
    if not spec.bot_pins:
        return ()

    creator = await session.get(Actor, task.creator_actor_id)
    if creator is None:  # pragma: no cover - a task always has a creator
        return ()
    found: list[ResolvedBot] = []
    for pin in spec.bot_pins:
        try:
            found.append(
                await bot_ops.resolve(
                    session, creator, str(pin.name), version=pin.version, project_id=task.project_id
                )
            )
        except Exception:
            logfire.warn("bot pin {ref} no longer resolves for step", ref=pin.ref())
    return tuple(found)
