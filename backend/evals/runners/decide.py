# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The decision runner: a real `AgentDef`, built through `build()`, run in this process on one row.

The decision is the run's first ending or its first acting tool call, whichever comes first. What a
call can change is the tool catalog's `SideEffect`: a pure tool runs as it is, a read answers with
the row's own fixture for that tool, and any other call is the decision, stopped before it acts.
A read the row has no fixture for raises `MissingFixture`: the row is reported as not run, since a
canned answer would score the agent's reaction to junk.

Offline (the unit tests) the model is a `FunctionModel`; the eval seat passes none, and the enabled
catalog serves the definition's model class exactly as a step's build does. Every call the meter
bills lands on the trace, never on the ledger. A decision is scored on its outcome alone, and a
text answer also on the row's checks and limits (`decided_well`), since only a text answer is the
whole of what the person gets.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, assert_never

from pydantic_ai import DeferredToolRequests, capture_run_messages
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models import Model

from evals.evaluators import ANSWERED_IN_TEXT, passes
from evals.evaluators.outcome import route_cost
from evals.runners import RowIncomplete, Runner, Trial
from evals.runners.live import earlier_turns, setup_gap
from evals.schema import Outcome, OutcomeRow
from evals.trace import PROPOSAL_TOOLS, PrismoidTrace, TokenCounts, ToolRecord, drives_a_computer
from prismoid.ai import config
from prismoid.ai.agents.base import build
from prismoid.ai.agents.bot_compile import seat_table, seatable
from prismoid.ai.agents.decision import Acted, ReadAnswer, decision_only
from prismoid.ai.agents.lead import ending_of
from prismoid.ai.deps import TaskDeps
from prismoid.ai.frames.paper import PaperFrame
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.tools import catalog
from prismoid.ai.tools.providers.search import search_chain
from prismoid.operations.execution import lead_rung
from prismoid.operations.execution.express import ambient_facts, express_prompt
from prismoid.operations.planning_brief import WebReach
from prismoid.schemas.bots import ResolvedBot
from prismoid.schemas.ids import TaskId
from prismoid.schemas.lead import (
    AskUser,
    Decline,
    Deliver,
    Fail,
    LeadEndingKind,
    LeadFacts,
    ProposeTask,
    SubmitPlan,
    TurnOrigin,
)
from prismoid.schemas.plan import GateKind, PlanFacts
from prismoid.schemas.side_effects import SideEffect
from prismoid.schemas.thread import EntryKind, ThreadEntry, ThreadTranscript, ThreadTurn
from prismoid.schemas.usage import Unit, UsageRecord

LEAD: Final = lead_rung.LEAD
_PAPER_BYTES: Final = 1 << 20  # the stub frame holds nothing: every call that would touch it is stopped
_THREAD: Final = TaskId(uuid.UUID(int=0))  # the row's thread belongs to no task


class MissingFixture(RowIncomplete):
    """The agent read with a tool the row has no fixture for: the row is incomplete, not the agent wrong."""


@dataclass(frozen=True)
class Seat:
    """The models a decision runs on: the enabled catalog and the routing table's chains."""

    catalog: list[ModelSpec]
    order: dict[str, list[str]] | None = None


def _fixtures(row: OutcomeRow) -> ReadAnswer:
    """A read's answer from the row's fixture for that tool; a read without one leaves the row not run."""

    def answer(name: str) -> object:
        if (fixture := row.fixtures.get(name)) is None:
            raise MissingFixture(f"missing fixture: the agent read with {name}")
        return fixture

    return answer


@dataclass(frozen=True)
class DecideRunner:
    """One row, one decision: `agent` built on `seat`, its tools stubbed; `model` replaces the seat's
    models offline. `bots` is the task's roster, for an agent that is a bot; `facts` the row's turn
    facts, for a seat that decides a turn (they ride its deps and close its prompt)."""

    agent: str
    seat: Seat
    model: Model | None = None
    bots: tuple[ResolvedBot, ...] = ()
    facts: Callable[[OutcomeRow], LeadFacts] | None = None

    async def __call__(self, row: OutcomeRow, /) -> PrismoidTrace:
        billed: list[UsageRecord] = []
        facts = None if self.facts is None else self.facts(row)
        deps = _deps(billed, self.bots, facts)
        runner = build(self.agent, deps, self.seat.catalog, order=self.seat.order)
        reads: list[str] = []
        started = time.monotonic()
        turn = (
            None
            if facts is None
            else lead_rung.lead_turn(facts, seatable(self.bots), skills={}, reach=_reach())
        )
        with decision_only(runner, _fixtures(row), reads, self.model), capture_run_messages() as messages:
            try:
                ending: object = (await runner.run(prompt_of(row, turn))).output
            except Acted as acted:
                ending = acted
        return _trace(ending, reads, messages, billed, time.monotonic() - started)


def lead(seat: Seat, model: Model | None = None) -> Runner:
    """The Lead, deciding each row as an attended turn in the app."""
    return DecideRunner(LEAD, seat, model, facts=attended)


def attended(row: OutcomeRow) -> LeadFacts:
    """A row's turn as intake sees a person's turn in the app: its number, nothing set."""
    return LeadFacts(turn=len(earlier_turns(row)) + 1, origin=TurnOrigin.WEB)


async def seat() -> Seat:
    """The eval seat's models, read as a step reads them."""
    from prismoid.db.session import session_scope
    from prismoid.operations import models as model_ops
    from prismoid.operations import routing as routing_ops

    async with session_scope() as session:
        return Seat(await model_ops.catalog(session, enabled_only=True), await routing_ops.order_for(session))


def gap(row: OutcomeRow, _connected: frozenset[str] | None = None) -> str | None:
    """Why a decision cannot be asked of this row, or None: the live runner's gaps with nothing
    connected, and the dials, which the door reads before any agent decides."""
    if row.context.dials:
        return "dials are the door's, decided before any agent runs"
    return setup_gap(row, frozenset())


def decided_well(trial: Trial) -> bool:
    """The outcome is one the row accepts; a text answer also passes the row's checks and limits."""
    trace = trial.trace
    if trace is None:
        return False
    if trace.outcome in ANSWERED_IN_TEXT:
        return passes(trial.row, trace)
    return route_cost(trial.row, trace) == 0


def _reach() -> WebReach:
    """The web's road as the roster says it: a configured search engine (the road `build` mounts), else none;
    a decision runs on no task, so no browser road is told."""
    return WebReach.SEARCH if search_chain() else WebReach.NONE


def prompt_of(row: OutcomeRow, turn: lead_rung.LeadTurn | None = None) -> str:
    """The row as the express rung builds its prompt: the ask, the thread before it, the ambient facts, and
    for a Lead `turn` the rung's own tail (the roster, the ambient facts, the turn note last)."""
    earlier: list[tuple[EntryKind, str]] = [(t.role, t.text) for t in row.prior_turns]
    asked: list[tuple[EntryKind, str]] = [("user", text) for text in (*row.context.turns, row.message)]
    lines = [*earlier, *asked]
    turns = [
        ThreadTurn(
            task_id=_THREAD,
            turn=n,
            entries=[ThreadEntry(kind=kind, text=text, task_id=_THREAD, turn=n, seq=n, tokens=0)],
        )
        for n, (kind, text) in enumerate(lines, start=1)
    ]
    transcript = ThreadTranscript(turns=turns, current_ask=row.message)
    ambient = ambient_facts(
        None, dt.datetime.now(dt.UTC), services=(), computer=config.current().computer_use.enabled
    )
    tail = ambient if turn is None else lead_rung.tail(turn, ambient)
    return express_prompt(row.message, transcript, ambient=tail)


def _deps(billed: list[UsageRecord], bots: tuple[ResolvedBot, ...], facts: LeadFacts | None) -> TaskDeps:
    """Deps that mount every tool a definition holds: a paper frame, and closures no stubbed run calls."""
    return TaskDeps(
        lead_facts=facts,
        plan_facts=PlanFacts(seats=seat_table(seatable(bots))),
        frame=PaperFrame("decide", max_bytes=_PAPER_BYTES),
        emit_usage=_kept(billed),
        propose_routine=_unreached,
        propose_task=_unreached,
        bots=bots,
    )


def _kept(billed: list[UsageRecord]) -> Callable[[UsageRecord], Awaitable[None]]:
    async def keep(record: UsageRecord) -> None:
        billed.append(record)

    return keep


async def _unreached(_payload: Mapping[str, Any]) -> str:
    """An acting tool's hands: the stub stops every act before it reaches them."""
    raise AssertionError("a stubbed run reached an acting tool's hands")


def _trace(
    ending: object, reads: list[str], messages: list[ModelMessage], billed: list[UsageRecord], seconds: float
) -> PrismoidTrace:
    """The decision as a trace: what it decided, what it said, what it read and what it cost."""
    responses = [m for m in messages if isinstance(m, ModelResponse)]
    outcome = _outcome(ending, reads)
    said = _said(ending, responses)
    return PrismoidTrace(
        outcome=outcome,
        delivered=outcome is not Outcome.ASK and not isinstance(ending, Fail),
        final_text=said,
        calls_before_delivery=len(responses),
        tokens=TokenCounts(
            input=_units(billed, Unit.INPUT_TOKEN),
            output=_units(billed, Unit.OUTPUT_TOKEN),
            cache_read=_units(billed, Unit.CACHE_READ_TOKEN),
            cache_write=_units(billed, Unit.CACHE_WRITE_TOKEN),
        ),
        cost_usd=sum((r.cost_usd for r in billed), Decimal(0)),
        tool_calls=tuple(ToolRecord(tool=tool, ok=True) for tool in reads),
        gates=(GateKind.INPUT,) if outcome is Outcome.ASK else (),
        latency_s=seconds,
    )


def _outcome(ending: object, reads: list[str]) -> Outcome:
    """The first act's outcome, or the ending's: an unattended result or failure ends in text, like a reply."""
    if isinstance(ending, Acted):
        return _acted(ending.tool)
    if isinstance(ending, DeferredToolRequests):
        return Outcome.ASK
    if not isinstance(ending, str | SubmitPlan | AskUser | ProposeTask | Fail | Deliver | Decline):
        raise TypeError(f"the decision runner cannot read a {type(ending).__name__} ending")
    match kind := ending_of(ending).ending:
        case LeadEndingKind.REPLY | LeadEndingKind.DELIVER | LeadEndingKind.FAIL | LeadEndingKind.DECLINE:
            return Outcome.LIGHT_WORK if reads else Outcome.REPLY
        case LeadEndingKind.PLAN:
            return Outcome.PLAN
        case LeadEndingKind.ASK:
            return Outcome.ASK
        case LeadEndingKind.PROPOSE:
            return Outcome.PROPOSE
        case _:
            assert_never(kind)


def _acted(tool: str) -> Outcome:
    """What the first act means for the person: a question, a proposal, or work started."""
    if catalog.side_effect_of(tool) is SideEffect.DEFERRAL:
        return Outcome.ASK
    if tool in PROPOSAL_TOOLS:
        return Outcome.PROPOSE
    return Outcome.PLAN_WITH_MACHINE if drives_a_computer(tool) else Outcome.PLAN


def _said(ending: object, responses: list[ModelResponse]) -> str:
    """What the person reads now: the answer, an offer's answer, a plan's line, else the text beside the act."""
    match ending:
        case str():
            return ending
        case ProposeTask(answer=answer):
            return answer
        case SubmitPlan(say=say):
            return say
        case Deliver(result=result):
            return result
        case Fail(reason=reason):
            return reason
        case Decline(answer=answer):
            return answer
        case _:
            return _text_of(responses)


def _text_of(responses: list[ModelResponse]) -> str:
    """What the model said beside its act: the text of its last response."""
    if not responses:
        return ""
    return "".join(p.content for p in responses[-1].parts if isinstance(p, TextPart))


def _units(billed: list[UsageRecord], unit: Unit) -> int:
    return sum(r.units.get(unit, 0) for r in billed)
