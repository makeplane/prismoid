# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The attempt's third stage: the agent, built and run. `prepare` builds the agent; `run_agent` runs
it on the attempt's call log - resuming a parked run with the human's decision (`answers.answer_for`
on the ending its replay stops on), continuing a guarded step's stopped run by one line
(`record.OWED_RUN`), or telling a fresh run its contract - inside the step's wall clock, and reads
its typed ending.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from typing import Any

from pydantic_ai.messages import ModelMessagesTypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.agents import base as agents_base
from prismoid.ai.agents.base import AgentDef, definition_for
from prismoid.ai.agents.run import AgentRunner, Cached, Continue, FromHistory, Resume, Start
from prismoid.ai.capabilities.durability import CallLogScope
from prismoid.ai.context import retry
from prismoid.ai.context.blocks import render_context_md
from prismoid.ai.context.brief import prompt_for
from prismoid.ai.context.citations import sources_brief
from prismoid.ai.context.inline import included_section
from prismoid.ai.frames.driver import FrameDriver
from prismoid.ai.frames.layout import INSTRUCTIONS, FramePaths, input_path
from prismoid.ai.frames.layout import render as render_paths
from prismoid.ai.frames.on_demand import OnDemandFrame
from prismoid.ai.frames.paper import PaperFrame
from prismoid.ai.tools.ask import CONNECT_GRANT
from prismoid.ai.tools.browser import PAGE_RESET
from prismoid.db.models.gate import Gate
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.operations import context as context_ops
from prismoid.operations import live, machines
from prismoid.operations import longform as longform_ops
from prismoid.operations import models as model_ops
from prismoid.operations import routing as routing_ops
from prismoid.operations import thread as thread_ops
from prismoid.operations.call_log import PgCallLog
from prismoid.operations.execution import inline
from prismoid.operations.execution.acquire import acquire as claim
from prismoid.operations.execution.acquire import phases_for
from prismoid.operations.execution.answers import answer_for, answers_to, v1_deferral, v1_requests
from prismoid.operations.execution.caps import MAX_STEP_HANDOFF_GATES
from prismoid.operations.execution.device import Cut, cut_note, journal_told, told_owed, wall_clock_stop
from prismoid.operations.execution.equip import Bench, Equipped
from prismoid.operations.execution.mounts import own_prior_artifacts, upstream_filename
from prismoid.operations.execution.record import owe_run, owed_run
from prismoid.operations.execution.settle import Ending, Settled
from prismoid.operations.execution.taps import record_prompt
from prismoid.schemas.guard import BUDGET_CONTINUE
from prismoid.schemas.ids import StepId
from prismoid.schemas.machines import retry_note
from prismoid.schemas.park import V1Park, park_of
from prismoid.schemas.plan import GateKind, StepSpec
from prismoid.schemas.steering import SteerNote, injection_text


def frame_for(acquired: machines.Acquired) -> FrameDriver:
    """The frame a step's tools hold: on paper, the paper until a tool needs the Machine."""
    return OnDemandFrame(acquired.frame) if isinstance(acquired.frame, PaperFrame) else acquired.frame


@dataclass
class MachineOnDemand:
    """`TaskDeps.machine` for a step on paper: its Machine, acquired once on the step's own session
    (idle across the run, so no row is held: the Machine row, then the task row, as every acquire)."""

    session: AsyncSession
    task: Task
    step: PlanStep
    bench: Bench
    frame: OnDemandFrame
    wire: live.Wire  # the acquire's phases narrate on the step's channel, as an up-front one does
    once: asyncio.Lock = field(default_factory=asyncio.Lock)  # two calls in one batch start one Machine

    async def acquire(self) -> FrameDriver:
        """Bind or start the Machine, carry the paper's files onto it, and hold it as the step's frame."""
        async with self.once:
            if self.frame.on_paper:
                await self._move()
        return self.frame.current

    async def _move(self) -> None:
        """The acquire, the carry and the swap; commits the step's session, which holds no row after."""
        phases = phases_for(self.wire, live.activity_emitter(self.wire), seat=False)
        bench = self.bench
        got = await claim(
            self.session,
            self.task,
            self.step,
            profile=bench.profile,
            allowed=bench.allowed,
            resume=None,
            seat=False,
            phases=phases,
        )
        try:  # fail-closed: a Machine the paper never reached is given back, never left held
            await phases.close(got.label)
            await self.frame.paper.carry_to(got.frame)
        except BaseException:
            await self._give_back(got)
            raise
        for held in fields(machines.Acquired):  # the attempt's `finally` now releases the Machine
            setattr(self.bench.acquired, held.name, getattr(got, held.name))
        self.frame.swap(got.frame)
        await self.session.commit()

    async def keep(self) -> None:
        """A park holding a draft the agent wrote moves to the Machine, which keeps it for the resume."""
        if self.frame.on_paper and self.frame.paper.drafts:
            await self.acquire()

    async def _give_back(self, got: machines.Acquired) -> None:
        """A Machine the files never reached goes back idle, its warm time billed."""
        segment = await machines.release(got, parked=False)
        if segment is not None:
            await machines.bill(self.session, segment)
        await self.session.commit()


@dataclass(frozen=True)
class Prepared:
    """The runner the step runs, the definition it was built from, its fleet seat's row, and the
    Machine a step on paper acquires on demand."""

    runner: AgentRunner
    definition: AgentDef
    model_ref: str | None
    can_hand_off: bool
    machine: MachineOnDemand | None = None

    async def parked(self) -> bool:
        """The run parks and keeps its frame; a draft on paper moves to the Machine first. True."""
        if self.machine is not None:
            await self.machine.keep()
        return True


async def prepare(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    spec: StepSpec,
    *,
    equipped: Equipped,
    bench: Bench,
    wire: live.Wire,
) -> Prepared:
    """The agent, on the class's chain (a fleet seat pins its row), with its Machine on demand when
    the step starts on paper. The catalog and routing reads ride the step's session, which the
    caller commits before the run."""
    catalog = await model_ops.catalog(session, enabled_only=True)
    order = await routing_ops.order_for(session)  # the routing table breaks the alphabetical tie
    # `quality` rides only the STEP path: the judge/planner/light lanes never pass
    # one, which is the whole exemption mechanism - verification integrity is not a cost dial.
    deps = equipped.deps
    machine = None
    if isinstance(bench.frame, OnDemandFrame):
        deps.machine = machine = MachineOnDemand(session, task, step, bench, bench.frame, wire)
    definition = definition_for(str(spec.agent), deps)
    # Looked up on the module per call: the builder is the engine's seam, swapped whole in tests.
    runner = agents_base.build(
        str(spec.agent),
        deps,
        catalog,
        order=order,
        settings=await routing_ops.settings_for(session),
        pin=spec.model_ref,
        quality=task.quality,
    )

    # Can a sign-in block still be handed to a person? Only on a Machine
    # with a screen or a device's own window, and only while the
    # step's hand-off budget lasts: past it a hand-off answers itself (OUT_OF_HANDOFFS) and a
    # nudge toward one would loop the run, nudge after nudge.
    can_hand_off = bench.acquired.hands_off and (
        await Gate.count_for_step(session, step.id, kind=GateKind.CONTROL.value, source="handoff")
        < MAX_STEP_HANDOFF_GATES
    )
    return Prepared(runner, definition, spec.model_ref, can_hand_off, machine)


@dataclass(frozen=True)
class Brief:
    """What a fresh or resumed run is told: the step's own context for one attempt."""

    spec: StepSpec
    gotchas: list[str]
    resume: dict[str, object] | None
    owed_cut: Cut
    fresh: bool  # a new attempt; False re-enters the run a worker died in, from its call log


async def run_agent(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    *,
    brief: Brief,
    bench: Bench,
    equipped: Equipped,
    prepared: Prepared,
) -> tuple[Any, Settled]:
    """**The step's total wall clock.** CLASS_TIMEOUT_S bounds one model REQUEST; this
    bounds the whole agent run - a wedged tool loop used to hold a queue slot indefinitely, and a
    frozen slot is indistinguishable from a busy one from outside. A timeout is an
    INFRA-flavoured rung on the ladder, never a dead task.

    Then **the typed ending, read**. A guarded agent finishes with `done(Outcome)`, checked inside
    the run (`Ending`, the output checks): a `done` is proof-checked before it counts, and a sign-in
    block on a Machine with a screen is told to hand off - one more request of the same run at most.
    A deferral that request makes parks like any."""
    envelope = prepared.runner.envelope
    browser = bench.frame.browser if bench.profile == "browser" else None
    acceptance = tuple(brief.spec.contract.acceptance) if brief.spec.contract is not None else ()
    ending = Ending(bench.frame, browser, prepared.can_hand_off, str(step.id), acceptance)
    equipped.deps.ending_checks = ending
    try:
        if brief.resume is not None:
            log, start = await _resumed(task, step, brief.resume, bench=bench)
        else:
            log = await call_log_for(task, step, fresh=brief.fresh)
            start = await _start(
                session, task, step, log, brief=brief, bench=bench, equipped=equipped, prepared=prepared
            )
        cancelled = equipped.inbox.cancelled  # a cancel cuts the run within a poll
        result = await envelope.within(
            equipped.deps, prepared.runner.run(start, log=log, cancelled=cancelled)
        )
    except TimeoutError:
        # A wall-clock stop is an INFRA-flavoured rung, never a dead task and
        # never a model failure: `infra=True` nets it out of `model_attempts()` - a
        # wedged tool loop must not spend the user's ladder budget. On the device its command
        # may still run: the step parks instead.
        raise wall_clock_stop(task, envelope.wall_s) from None
    settled = ending.settled(result, prepared.runner.checked)
    return settled.result, settled


async def _start(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    log: CallLogScope,
    *,
    brief: Brief,
    bench: Bench,
    equipped: Equipped,
    prepared: Prepared,
) -> Start:
    """What the attempt runs, its receipt on the record first: the run a guarded step's last attempt
    stopped at a logged point, continued by **one line saying why** (never the contract re-told, so
    the model keeps what it saw and did), or the prompt - which a re-entered run replays as logged.
    What a death owed (`OwedRun`) is consumed here, on the receipt's commit, success or failure."""
    owed = owed_run(step)
    told_owed(step)  # the receipt below tells it: owed no more
    owe_run(step, None)
    if owed is not None and owed.pickup == "continue" and prepared.definition.guarded and log.segments():
        told = continue_line(owed.reason, owed.steers, step, bench.acquired.changed, cut_note(brief.owed_cut))
        start: Start = Continue(f"attempt:{step.attempt}", told)
    else:
        told = await _prompt(
            session, task, step, brief=brief, bench=bench, equipped=equipped, prepared=prepared
        )
        start = told
    # **The prompt goes on the record before the run**. "Why did my bot do that" is
    # unanswerable from narrations alone - the answer is the prompt: the goal as retold, the
    # answers folded in, the skills named, the gotchas. Written BEFORE the run so a wedged or
    # killed attempt still shows what it was told, and committed here, so the step's session sits
    # committed and idle across the run (found live: a slow DM turn showed no step.prompt row while
    # running).
    said = f"{told.head}{told.tail}" if isinstance(told, Cached) else told
    await record_prompt(session, task, step, str(brief.spec.agent or ""), said, model_ref=prepared.model_ref)
    await session.commit()
    await journal_told(bench.frame, brief.owed_cut)
    return spoken(start, bench.frame.paths)


def spoken(start: str | Continue | Cached, paths: FramePaths) -> Start:
    """The start as the agent reads it, spelled in the frame's own paths: the record keeps the virtual
    roots (nothing stored holds a host path)."""
    if isinstance(start, Continue):
        return Continue(start.key, render_paths(start.line, paths))
    if isinstance(start, Cached):
        return Cached(render_paths(start.head, paths), render_paths(start.tail, paths))
    return render_paths(start, paths)


def continue_line(
    reason: str, steers: Sequence[SteerNote], step: PlanStep, changed: Sequence[str] | None, note: str | None
) -> str:
    """A continued attempt's one line (`retry.continue_prompt`): why it stopped, with the steers taken
    since and the Machine's change list riding along, like a fresh retry's, and what a crash cut off."""
    told = retry.continue_prompt(reason)
    if steers:  # taken at this attempt's start: the contract that carries them is not re-told
        told += "\n\n" + injection_text(steers)
    if changed is not None:
        told += "\n\n" + retry_note(step.attempt, changed)
    if note is not None:
        told += "\n\n" + note
    return told


async def _resumed(
    task: Task, step: PlanStep, resume: dict[str, object], *, bench: Bench
) -> tuple[CallLogScope, Start]:
    """The parked run, resumed on its call log: the same generation replayed to the ending it stopped
    on, which the gate's resolution answers as one more segment (`gate:<id>`, or `in-place:<n>` for a
    call answered without a gate). A park saved before the call log seeds a fresh generation with its
    saved conversation instead."""
    park = park_of(resume)
    # An answer that comes back to a browser the Machine's sleep restarted says so first; an
    # approved call's own result ends with it (`BrowserPorts.page_reset`).
    reset = f"{PAGE_RESET}\n\n" if bench.acquired.woke_from_park and bench.profile == "browser" else ""
    v1 = V1Park.of(resume)
    if v1 is not None:
        return await call_log_for(task, step, fresh=True), _v1_start(v1, resume, reset)
    log = await call_log_for(task, step, fresh=False)
    key = gate_key(park.gate_id) if park.gate_id is not None else f"in-place:{len(log.segments())}"
    return log, Resume(key, lambda ending: answer_for(park, ending, resume, reset=reset))


def _v1_start(v1: V1Park, resume: dict[str, object], reset: str) -> FromHistory:
    """A v1 park's saved conversation (stored latin-1, losslessly), answered by the ids it saved - or
    continued by one line, for a budget park that held no call."""
    history = ModelMessagesTypeAdapter.validate_json(v1.history.encode("latin-1"))
    deferral = v1_deferral(v1)
    if deferral is None:
        return FromHistory(history, None, f"{reset}{BUDGET_CONTINUE}")
    return FromHistory(history, answers_to(deferral, v1_requests(history), resume, reset=reset), "")


def gate_key(gate_id: uuid.UUID) -> str:
    """The segment a gate's answer is logged as."""
    return f"gate:{gate_id}"


async def answer_logged(task: Task, step: PlanStep, gate_id: uuid.UUID) -> bool:
    """Whether the step's call log holds the gate's answer: a resume of it already ran."""
    log = PgCallLog(task.workspace_id)
    latest = await log.latest(StepId(step.id))
    if latest.key is None:
        return False
    segments = CallLogScope(log, latest.key, dict(latest.entries)).segments()
    return any(s.key == gate_key(gate_id) for s in segments)


async def call_log_for(task: Task, step: PlanStep, *, fresh: bool) -> CallLogScope:
    """The attempt's call log: a new generation, or the one a recovered run re-enters (ADR-089)."""
    return await CallLogScope.open(PgCallLog(task.workspace_id), StepId(step.id), fresh=fresh)


async def _prompt(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    *,
    brief: Brief,
    bench: Bench,
    equipped: Equipped,
    prepared: Prepared,
) -> str | Cached:
    """**Tell it what it actually has.** `spec.contract.inputs` was written at *plan* time, when
    the upstream step had not run and its findings did not exist. `inputs` is what is on disk
    *now*. An agent not told a file exists will not read it - it writes a plausible memo from the
    sources it *was* told about, and the research step it depended on becomes an expense with a
    narration attached (`prompt_for` says exactly this, and it was right)."""
    spec, owed_cut = brief.spec, brief.owed_cut
    told, tail = spec, None
    if spec.lane_role == "section" and spec.contract is not None:
        told, tail = await _part_brief(session, task, step, spec, equipped.inputs)
    elif spec.contract is not None:
        told = spec.model_copy(
            update={"contract": spec.contract.model_copy(update={"inputs": list(equipped.inputs)})}
        )
    # The skills and small text inputs it was given ride IN the brief: no read round trip.
    included = await inline.included_files(session, task, told) if told.contract is not None else []
    prompt = prompt_for(
        told,
        brief.gotchas,
        connectors=equipped.deps.connectors,
        # Bot-aware on purpose (the `publishes_text` lesson): REGISTRY alone KeyErrors
        # on a bot-served step; `deps` carries this step's bots.
        asker=CONNECT_GRANT in prepared.definition.tools,
        guarded=prepared.definition.guarded,
        interruption=owed_cut.head,
        still_running=owed_cut.still_running,
        exit_code=owed_cut.exit_code,
        inlined=[f.path for f in included],
        revising=[input_path(upstream_filename(a)) for a in await own_prior_artifacts(session, task)],
        today=await thread_ops.author_today(session, task),
    )
    # A toolless agent cannot open /task/context.md, so for it the remembered context inlines
    # into the prompt instead of mounting out of reach (a DM keeper with no
    # frame.read_file answered a question its own ledger held, from model weights). File-capable
    # agents keep the mount - the prompt already names it, and inlining twice would double-spend
    # tokens.
    if spec.agent and ("frame.read_file" not in definition_for(str(spec.agent), equipped.deps).tools):
        prompt += await inline_context(session, task)
    moving = sources_brief(equipped.deps.sources)  # a citing step's earlier pages, by id
    if bench.acquired.changed is not None:
        # A retry in the same Machine is shown what the failed attempt changed.
        moving += "\n\n" + retry_note(step.attempt, bench.acquired.changed)
    if tail is not None:  # a long document's part: the head every part shares, then its own tail
        return Cached(prompt + included_section(included), tail + moving)
    return prompt + moving + included_section(included)  # last: the variable text after the stable brief


async def _part_brief(
    session: AsyncSession, task: Task, step: PlanStep, spec: StepSpec, inputs: Sequence[str]
) -> tuple[StepSpec, str]:
    """A part's brief split in two: the spec every part shares (its plan-time sources and the task's own
    mounts, no assignment), and its tail (its assignment, the parts before it, what else is mounted)."""
    assert spec.contract is not None
    shared = [p for p in inputs if p in spec.contract.inputs or p in INSTRUCTIONS]
    extra = [p for p in inputs if p not in shared]
    tail = await longform_ops.part_tail(session, task, step, spec.contract.notes)
    if extra:
        tail += "\n\nAlso mounted for you: " + ", ".join(f"`{p}`" for p in extra)
    contract = spec.contract.model_copy(update={"inputs": shared, "notes": ""})
    return spec.model_copy(update={"contract": contract}), tail


async def inline_context(session: AsyncSession, task: Task) -> str:
    """The remembered context, inlined for an agent that cannot open /task/context.md - read
    through the same fence as the mount, so a Project task's prompt never carries
    a private lane, whoever stored the pack. Empty when there is nothing to say."""

    pack = await context_ops.readable_pack(session, task)
    return "" if pack is None else "\n\n" + render_context_md(pack)
