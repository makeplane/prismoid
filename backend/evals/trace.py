# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`PrismoidTrace`: one turn, reduced to the facts every evaluator reads.

Every runner (live, a benchmark adapter) ends in the same step: hand the task's
durable events to `PrismoidTrace.from_events`. Nothing is scored from logs and nothing is scored from a
runner's own clock except what the durable record cannot hold (the first streamed delta, the
client's submit time), which the runner passes in.

The outcome is classified from what the turn *did*, in order, and the first decisive fact wins:

    propose            a proposal landed (`task.proposed`, or a proposal tool that succeeded)
    ask                a question or connect gate opened before any work began: a work tool's
                       call or an action held for approval
    plan_with_machine  a plan with work steps ran and a step drove a browser or a shell
    plan               a plan with work steps ran
    light_work         delivered with no plan, after at least one tool call
    reply              delivered with no plan and no tool call
    None               no plan of work and no delivery

`reject` (the API refused the ask, so there is no task) has no events; it is `PrismoidTrace.refused()`.
How the turn was routed is carried as `route` and `route_source` for reading and never decides
anything: the classifier reads steps, tools and gates, so it survives every rewrite of the door. A
plan's declared Machine need (`task.planned.machine`) decides nothing either: a researcher holds a
shell it may never use, so only a browser or shell call that ran makes a plan `plan_with_machine`.

`calls_before_delivery` counts the model calls the answer waited for. The title call is not one: the
namer runs beside the clarifier, never on the answer's path, and its `usage.recorded` is the event
right before the `task.renamed` it lands in the same transaction.

Tool names are the journal's own, imported from the tools that journal them: a connector's call
is `{slug}_{tool}`, and a shell on the Machine counts as driving a computer, like the browser.
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from evals.schema import Outcome, Unscored
from prismoid.ai.tools.browser import BROWSER_TOOLS
from prismoid.ai.tools.catalog import journal_name
from prismoid.schemas.events import (
    ArtifactProduced,
    DurableEvent,
    DurableEventAdapter,
    GateOpened,
    MachineCreated,
    MemoryWritten,
    StepNarration,
    TaskClarifying,
    TaskContinued,
    TaskCreated,
    TaskDelivered,
    TaskPlanned,
    TaskProposed,
    TaskRenamed,
    ToolCalled,
    UsageRecorded,
)
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import GateKind, PlanView
from prismoid.schemas.usage import Unit

# Machine seconds and search queries bill under their own usage family; every other row is a model call.
NOT_MODEL_CALLS: frozenset[str] = frozenset({"machine", "web"})

# A journaled call that is the turn's ending, not work done: it proposes or escalates.
PROPOSAL_TOOLS: frozenset[str] = frozenset({journal_name("propose_task"), journal_name("routine_create")})
# A recorded trace from before the Lead may still end on the retired answerer's escalation.
ENDING_TOOLS: frozenset[str] = PROPOSAL_TOOLS | {"escalate_to_plan"}

# The one step a turn that answered for itself plans. Any other step is work.
TALK_LANE_ROLES: frozenset[str] = frozenset({"express"})

# Journal names that mean the step drove a computer: a browser, a shell on the Machine.
COMPUTER_NAMESPACES: frozenset[str] = frozenset(tool.partition(".")[0] for tool in BROWSER_TOOLS)
COMPUTER_TOOLS: frozenset[str] = frozenset({"frame.shell"})

# A question the turn stopped to ask, as opposed to an approval inside work already planned.
ASKING_GATES: frozenset[GateKind] = frozenset({GateKind.INPUT, GateKind.CONNECT})


class ToolRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool: str  # the journal name: `web.search`, `browser.open`, a connector's `slack_post_message`
    ok: bool
    duration_ms: int = 0

    @property
    def namespace(self) -> str:
        return self.tool.partition(".")[0]

    def is_named(self, name: str) -> bool:
        """Whether a row's name means this call: the journal name, its namespace (`browser`) or verb
        (`publish`)."""
        return name in (self.tool, self.namespace, self.tool.rpartition(".")[2])

    def calls_a_connector(self, slugs: Iterable[str]) -> bool:
        """Whether this is a call to one of the connectors `slugs`, journaled as `{slug}_{tool}`."""
        return any(self.tool.startswith(f"{slug}_") for slug in slugs)


class ArtifactRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    artifact_type: ArtifactType
    filename: str | None = None


class TokenCounts(BaseModel):
    """Model-call tokens in the ledger's native units: `input` is the uncached remainder."""

    model_config = ConfigDict(frozen=True)

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


class PrismoidTrace(BaseModel):
    """What one turn did and what it cost. Frozen; build it with `from_events`."""

    model_config = ConfigDict(frozen=True)

    outcome: Outcome | Unscored | None = None
    delivered: bool = False
    final_text: str = ""
    prior_text: str = ""  # the previous turn's final text, for the checks that compare against it

    calls_before_delivery: int = 0  # model calls the answer waited for: not the title, not the verdict
    tokens: TokenCounts = TokenCounts()
    cost_usd: Decimal = Decimal(0)  # all spend up to delivery (the whole turn when never delivered)

    tool_calls: tuple[ToolRecord, ...] = ()  # work only: an ending tool is a decision, not a call
    machines: int = 0  # `machine.created` this turn
    artifacts: tuple[ArtifactRecord, ...] = ()  # deliverables; working files are scaffolding
    gates: tuple[GateKind, ...] = ()  # opened this turn
    memory_written: tuple[str, ...] = ()
    connectors_auto: tuple[str, ...] = ()  # connectors attached on their own at create

    route: str | None = None  # diagnostic: never scored
    route_source: str | None = None  # diagnostic: never scored

    ttft_s: float | None = None
    latency_s: float | None = None

    @classmethod
    def refused(cls) -> PrismoidTrace:
        """The API refused the ask: no task, no events, nothing spent."""
        return cls(outcome=Unscored.REJECT)

    @classmethod
    def from_events(
        cls,
        events: Iterable[DurableEvent],
        *,
        turn: int = 1,
        ttft_s: float | None = None,
        submitted_at: dt.datetime | None = None,
    ) -> PrismoidTrace:
        """The trace of `turn`; `ttft_s` and `submitted_at` are the runner's clock, which the record
        cannot hold."""
        ordered = sorted(events, key=lambda event: event.seq)
        window = _turn_window(ordered, turn)
        delivered = next((e for e in window if isinstance(e, TaskDelivered)), None)
        ledger = _Ledger.of(window, delivered)
        planned = [e for e in window if isinstance(e, TaskPlanned)]
        start = window[0]
        return cls(
            outcome=_classify(window, delivered is not None),
            delivered=delivered is not None,
            final_text=_final_text(window),
            prior_text=_final_text(_turn_window(ordered, turn - 1)) if turn > 1 else "",
            calls_before_delivery=ledger.calls_before_delivery,
            tokens=ledger.tokens,
            cost_usd=ledger.cost_usd,
            tool_calls=tuple(
                ToolRecord(tool=e.tool, ok=e.ok, duration_ms=e.duration_ms)
                for e in window
                if isinstance(e, ToolCalled) and e.tool not in ENDING_TOOLS
            ),
            machines=sum(isinstance(e, MachineCreated) for e in window),
            artifacts=tuple(
                ArtifactRecord(artifact_type=e.artifact_type, filename=e.filename)
                for e in window
                if isinstance(e, ArtifactProduced) and e.role == "deliverable"
            ),
            gates=tuple(e.kind for e in window if isinstance(e, GateOpened)),
            memory_written=tuple(
                item.text for e in window if isinstance(e, MemoryWritten) for item in e.items
            ),
            connectors_auto=next(
                (tuple(e.connectors_auto) for e in window if isinstance(e, TaskCreated)), ()
            ),
            route=planned[-1].route if planned else None,
            route_source=planned[-1].route_source if planned else None,
            ttft_s=ttft_s,
            latency_s=(delivered.at - (submitted_at or start.at)).total_seconds() if delivered else None,
        )


def events_from_payloads(payloads: Iterable[Mapping[str, Any]]) -> list[DurableEvent]:
    """Durable events from the record's JSON; strict, because a trace built from a guess scores one."""
    return [DurableEventAdapter.validate_python(payload) for payload in payloads]


def drives_a_computer(tool: str) -> bool:
    """Whether a journaled tool name is one that works a computer, not the web or the files."""
    return tool in COMPUTER_TOOLS or tool.partition(".")[0] in COMPUTER_NAMESPACES


def _turn_window(ordered: Sequence[DurableEvent], turn: int) -> list[DurableEvent]:
    """The events from the start of `turn` up to the start of the next one."""
    starts = [(i, _turn_started(e)) for i, e in enumerate(ordered) if _turn_started(e) is not None]
    for position, (index, started) in enumerate(starts):
        if started == turn:
            end = starts[position + 1][0] if position + 1 < len(starts) else len(ordered)
            return list(ordered[index:end])
    raise ValueError(f"the events hold no turn {turn}")


def _turn_started(event: DurableEvent) -> int | None:
    if isinstance(event, TaskCreated):
        return 1
    if isinstance(event, TaskContinued):
        return event.turn
    return None


def _final_text(window: Sequence[DurableEvent]) -> str:
    """The delivered message; for a turn that narrated and never delivered, its last narration."""
    for event in window:
        if isinstance(event, TaskDelivered):
            return event.summary
    narrations = [e.markdown for e in window if isinstance(e, StepNarration)]
    return narrations[-1] if narrations else ""


@dataclass(frozen=True)
class _Ledger:
    """The turn's spend up to delivery: what the answer cost, and the model calls it waited for."""

    calls_before_delivery: int
    tokens: TokenCounts
    cost_usd: Decimal

    @classmethod
    def of(cls, window: Sequence[DurableEvent], delivered: TaskDelivered | None) -> _Ledger:
        rows = [e for e in window if isinstance(e, UsageRecorded)]
        calls = [e for e in rows if e.family not in NOT_MODEL_CALLS]
        titles = _title_calls(window)

        def early(row: UsageRecorded) -> bool:
            return delivered is None or row.seq < delivered.seq

        def tokens(unit: Unit) -> int:
            return sum(row.units.get(unit, 0) for row in calls)

        return cls(
            calls_before_delivery=sum(early(row) and row.seq not in titles for row in calls),
            tokens=TokenCounts(
                input=tokens(Unit.INPUT_TOKEN),
                output=tokens(Unit.OUTPUT_TOKEN),
                cache_read=tokens(Unit.CACHE_READ_TOKEN),
                cache_write=tokens(Unit.CACHE_WRITE_TOKEN),
            ),
            cost_usd=sum((row.cost_usd for row in rows if early(row)), Decimal(0)),
        )


def _title_calls(window: Sequence[DurableEvent]) -> frozenset[int]:
    """The seqs of the namer's model calls: each is the `usage.recorded` right before a `task.renamed`."""
    return frozenset(
        before.seq
        for before, after in itertools.pairwise(window)
        if isinstance(before, UsageRecorded)
        and isinstance(after, TaskRenamed)
        and after.seq == before.seq + 1
    )


def _has_work(plan: PlanView) -> bool:
    return any(step.lane_role not in TALK_LANE_ROLES for step in plan.steps)


def _classify(window: Sequence[DurableEvent], delivered: bool) -> Outcome | None:
    """The first decisive fact wins, in the order the turn did them."""
    work_planned = False
    drove_computer = False
    worked = False
    for event in window:
        if isinstance(event, TaskProposed):
            return Outcome.PROPOSE
        if isinstance(event, ToolCalled):
            if event.ok and event.tool in PROPOSAL_TOOLS:
                return Outcome.PROPOSE
            if event.tool not in ENDING_TOOLS:
                worked = True
                drove_computer = drove_computer or drives_a_computer(event.tool)
        elif isinstance(event, GateOpened) and event.kind is GateKind.APPROVE:
            worked = True
        elif not worked and (
            isinstance(event, TaskClarifying)
            or (isinstance(event, GateOpened) and event.kind in ASKING_GATES)
        ):
            return Outcome.ASK
        elif isinstance(event, TaskPlanned) and _has_work(event.plan):
            work_planned = True
    if work_planned:
        return Outcome.PLAN_WITH_MACHINE if drove_computer else Outcome.PLAN
    if not delivered:
        return None
    return Outcome.LIGHT_WORK if worked else Outcome.REPLY
