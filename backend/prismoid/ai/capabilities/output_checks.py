# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Output checks: a step's typed ending, checked inside the run that produced it.

When the run ends with an `Outcome` (the `done` tool), before it counts:

* a browser step that ended `failed` or `blocked` before it used its hands (no browser or shell call,
  neither a refusal nor a stop on the budget, an approval or a person) is told to drive first;
* a `blocked` on an approval the person declined is told their no is an answer: end `partial`;
* a `blocked` on a sign-in or a person, when a person can still be handed the browser, is told to
  hand it over (`browser_handoff`) - unless a hand-off already came back in this run (skipped, nobody
  came, or handed back): then it is told to take another way the goal names, never to hand over again;
* a `done` is checked, and a failed check is told its reasons, all in one nudge: its proof
  (`EndingChecks.proof`, a logged read of the call log: a replay reads what the live check saw), its
  goal (every line of the step's `acceptance` accounted for in `Outcome.criteria` and met), and its
  last guarded act (the act a `done` rests on landed: `ActCheck`, the browser's own facts). The goal
  and the act are read from typed values the run already holds, so page text can flip neither.

Each nudge is a `ModelRetry` from the output hook: one more model request in the same run, on the
same guard, limits and call log. At most `NUDGES` per run: drive-first once, then one of the
decline, the hand-off or the proof. The model's Outcome is never rewritten. Once the nudge is spent, or the run
has no room for one (its request ceiling, or less than one answering turn of its wall: a logged
read, so a replay sees the wall the live run saw), the ending stands, and what it stands on is left
in `Checked`, which the runner hands operations after the run (`AgentRunner.checked`), never on
`TaskDeps`: the failed proof (`unconfirmed`, a `partial` beside the model's word) and the block a
hand-off nudge answered (`handed_off`). Every nudge spends one of the agent's output retries, so `OUTPUT_RETRIES` is the
schema's own budget plus `NUDGES`: a nudge never takes a retry a fumbled schema needs. Every other check
that sends an ending back (`citations`, `record_sources`) draws at most `NUDGES` a run too, and only
while the pool keeps one (`may_send_back`): a noisy check never kills the attempt, its ending stands.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import KW_ONLY, dataclass, field, replace
from typing import Any, Final

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.capabilities import AbstractCapability, durable_operation
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.output import OutputContext

from prismoid.ai.deps import EndingChecks, TaskDeps
from prismoid.ai.rules.budget import requests_left
from prismoid.ai.tools import catalog
from prismoid.ai.tools.browser import TOOL_NAMES, act_of
from prismoid.ai.tools.call_rows import declined, ran
from prismoid.ai.tools.wall import MARGIN_S
from prismoid.schemas.computer_use import GUARDED_VERBS, ActCheck, Landed
from prismoid.schemas.guard import DECLINED_NUDGE, DRIVE_FIRST, HANDED_OVER_NUDGE, HANDOFF_NUDGE, proof_nudge
from prismoid.schemas.outputs import Outcome

NUDGES: Final = 2
SCHEMA_RETRIES: Final = 3  # a structured output is a tool call, and a model can fumble a schema
OUTPUT_RETRIES: Final = SCHEMA_RETRIES + NUDGES

_SHELL: Final = catalog.tool("shell").name  # a browser step's other hands (`frame_tools.shell`)
_HANDOFF: Final = catalog.tool("browser_handoff").name
# A stop that is a decision, not a guess: the budget is spent, or the next move is a person's.
# Pushing past one would cross the line it draws.
_DECIDED_STOPS: Final = frozenset({"budget", "approval", "person"})
_GAVE_UP: Final = frozenset({"failed", "blocked"})  # the endings a step can reach on a guess
_PERSONS: Final = frozenset({"login", "person"})  # the walls a person can take the browser past
_LOOK_ONLY: Final = "scroll"  # an act that changes no state: a `done` never rests on it
_SHOWN: Final = 120  # how much of a criterion or of the model's own words a reason repeats
_MISSES: Final[dict[Landed, str]] = {
    Landed.FAILED: "the browser refused it",
    Landed.UNCHANGED: "the page did not change",
}


def may_send_back(ctx: RunContext[TaskDeps], sent: int) -> bool:
    """Whether a check that sent back `sent` endings this run may send one more: its own `NUDGES`, a
    retry left in the shared pool, and a request left in the segment."""
    return sent < NUDGES and ctx.retry < ctx.max_retries and requests_left(ctx) != 0


def gave_up_undriven(outcome: Outcome, messages: Sequence[ModelMessage]) -> bool:
    """Whether an ending stopped short before the step used its hands: `failed`, or `blocked`
    on a guess, with no browser or shell call in the run's history."""
    if outcome.status not in _GAVE_UP or outcome.refused or outcome.blocked_by in _DECIDED_STOPS:
        return False
    return not any(
        isinstance(part, ToolCallPart) and (part.tool_name in TOOL_NAMES or part.tool_name == _SHELL)
        for message in messages
        if isinstance(message, ModelResponse)
        for part in message.parts
    )


def handed_over(messages: Sequence[ModelMessage]) -> bool:
    """Whether a hand-off came back in this run's history: the person already had their turn."""
    return any(
        ran(part) and part.tool_name == _HANDOFF
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
    )


def unmet_goal(acceptance: Sequence[str], outcome: Outcome) -> str | None:
    """Why `outcome` does not meet the lines the step is judged on: one not accounted for in its
    `criteria`, or one it says is not met. None: every line is met (or the step has none)."""
    told = {c.line: c for c in outcome.criteria}
    reasons: list[str] = []
    for n, line in enumerate(acceptance, start=1):
        said = told.get(n)
        if said is None:
            reasons.append(f"line {n} ({line[:_SHOWN]!r}) is not accounted for in `criteria`")
        elif not said.met:
            reasons.append(f"line {n} ({line[:_SHOWN]!r}) is not met: {said.shows[:_SHOWN]}")
    return "; ".join(reasons) or None


def last_act(messages: Sequence[ModelMessage]) -> ActCheck | None:
    """The check of the run's last browser act that could change state; None when it made none."""
    checks = (
        check
        for message in reversed(messages)
        if isinstance(message, ModelRequest)
        for part in reversed(message.parts)
        if ran(part) and part.tool_name in TOOL_NAMES and (check := act_of(part.metadata)) is not None
    )
    return next((c for c in checks if c.verb != _LOOK_ONLY), None)


def missed_act(messages: Sequence[ModelMessage]) -> str | None:
    """Why the act a `done` rests on did not land: the run's last state-changing act is a guarded
    one that the browser refused or that changed nothing. None: it landed, or there was none."""
    act = last_act(messages)
    if act is None or act.verb not in GUARDED_VERBS or not act.missed:
        return None
    return f"your last {act.verb} did not land ({_MISSES[act.landed]})"


@dataclass
class Checked:
    """What the checks left on the last run's ending, for operations to settle."""

    unconfirmed: str | None = None  # the failed check the run's `done` stands on
    handed_off: Outcome | None = None  # the block a hand-off nudge answered


@dataclass
class OutputChecks(AbstractCapability[TaskDeps]):
    """The checks on a run's Outcome; inert without `TaskDeps.ending_checks`."""

    id: str | None = "output_checks"  # pinned: the call log names its reads by it
    _: KW_ONLY
    checked: Checked = field(default_factory=Checked)  # every run's copy shares it with the runner
    _drove: bool = field(default=False, init=False)  # the drive-first nudge is spent
    _closed: bool = field(default=False, init=False)  # the decline, hand-off or proof nudge is spent

    async def for_run(self, ctx: RunContext[TaskDeps]) -> OutputChecks:
        """A fresh copy per run, and the run's verdicts start empty: they are the last run's."""
        self.checked.unconfirmed = self.checked.handed_off = None
        return replace(self)

    async def after_output_validate(
        self, ctx: RunContext[TaskDeps], *, output_context: OutputContext, output: Any
    ) -> Any:
        checks = ctx.deps.ending_checks
        if checks is None or ctx.partial_output or not isinstance(output, Outcome):
            return output
        if (nudge := await self._nudge(ctx, checks, output)) is not None:
            raise ModelRetry(nudge)
        return output

    async def _nudge(self, ctx: RunContext[TaskDeps], checks: EndingChecks, outcome: Outcome) -> str | None:
        """The line one more request is told, or None: the ending stands on what `checks` holds."""
        room = await self._room(ctx) and ctx.retry < ctx.max_retries
        if (
            room
            and not self._drove
            and ctx.deps.browser is not None
            and gave_up_undriven(outcome, ctx.messages)
        ):
            self._drove = True
            return DRIVE_FIRST
        self.checked.unconfirmed = None
        if outcome.status == "blocked" and outcome.blocked_by == "approval" and declined(ctx.messages):
            if room and not self._closed:
                self._closed = True
                return DECLINED_NUDGE
            return None
        if outcome.status == "blocked" and outcome.blocked_by in _PERSONS:
            if not room or self._closed:
                return None
            if handed_over(ctx.messages):
                self._closed = True
                return HANDED_OVER_NUDGE
            if checks.can_hand_off:
                self._closed = True
                self.checked.handed_off = outcome
                return HANDOFF_NUDGE
            return None
        if outcome.status != "done" or (reason := await self._done(ctx, checks, outcome)) is None:
            return None
        if room and not self._closed:
            self._closed = True
            return proof_nudge(reason)
        self.checked.unconfirmed = reason
        return None

    async def _done(self, ctx: RunContext[TaskDeps], checks: EndingChecks, outcome: Outcome) -> str | None:
        """Every reason a `done` fails its checks, joined; None when it holds."""
        reasons = (
            unmet_goal(checks.acceptance, outcome),
            missed_act(ctx.messages),
            await self._proof(ctx, outcome),
        )
        return "; ".join(r for r in reasons if r) or None

    @durable_operation(name="room")
    async def _room(self, ctx: RunContext[TaskDeps]) -> bool:
        """Whether the run can afford one more request (its ceiling, one answering turn of its wall), logged."""
        limit = ctx.usage_limits.request_limit if ctx.usage_limits is not None else None
        if limit is not None and ctx.usage.requests >= limit:
            return False
        deadline = ctx.deps.deadline
        return deadline is None or deadline - time.monotonic() > MARGIN_S

    @durable_operation(name="proof")
    async def _proof(self, ctx: RunContext[TaskDeps], outcome: Outcome) -> str | None:
        """Why `outcome`'s proof fails now (None: it holds), logged."""
        assert ctx.deps.ending_checks is not None  # `after_output_validate` asked only with checks
        return await ctx.deps.ending_checks.proof(outcome, ctx.messages)
