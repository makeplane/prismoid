# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The attempt's fourth stage: did the run stop without ending? A run the task's cap stopped, or
one whose turn deferred calls - to ask, to connect, for consent, for a person at the browser, or
for a pause - becomes the one `Deferral` its park saves. None means the run ended and its output
publishes. Which deferral wins is fixed: a person at the browser, then the cap, a question, a
connect request, approvals alone, and the pause. The cap is CHECKED first, but a turn carrying a
control call is never the cap's (`budget_deferral`): someone is at the browser.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Final

from pydantic_ai import DeferredToolRequests
from pydantic_ai.messages import ToolCallPart
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.context.thread import safe_name
from prismoid.ai.tools.ask import ASK_USER, REQUEST_CONNECTOR
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.task import Task
from prismoid.operations.execution.deferrals import (
    ConnectDeferral,
    ControlDeferral,
    Deferral,
    ParkDeferral,
    PauseDeferral,
    approval_deferral,
    budget_held,
    budget_park_of,
    control_calls,
    control_deferral,
)
from prismoid.operations.execution.inbox import StepInbox
from prismoid.operations.execution.settle import Settled
from prismoid.schemas.events import TaskStatusChanged
from prismoid.schemas.park import ParkKind, ParkRecord
from prismoid.schemas.plan import TaskStatus
from prismoid.schemas.steering import PAUSE_METADATA


async def classify(
    session: AsyncSession, task: Task, result: Any, *, settled: Settled, inbox: StepInbox, spend: Any
) -> ParkDeferral | None:
    """The park the run's ending asks for (a cap, a question, an approval and so on), or None when it ended.
    A turn with both an ask and approvals parks on the question; the approvals resolve denied-with-retry."""
    budget = await budget_park_of(result, told=inbox.told, spend=spend)
    if budget is not None:
        return budget
    output = result.output
    if not isinstance(output, DeferredToolRequests):
        return None
    paused = _paused(output)
    turn = _Turn(output, inbox.told, paused, await paused_by(session, task) if paused else "")
    control = _control(turn)
    if control is not None:
        return replace(control, ended=settled.blocked)
    return _question(turn) or _connect(turn) or _approvals(turn) or _pause(turn)


def deferral_of(park: ParkRecord, output: DeferredToolRequests) -> ParkDeferral:
    """The park a replayed run's deferred turn was, read again off the turn by the rule its park
    took: the ids its resume answers are the turn's own, never saved beside it."""
    if park.kind is ParkKind.BUDGET:
        return budget_held(output, told=park.told_epoch)
    turn = _Turn(output, park.told_epoch, _paused(output), park.paused_by)
    found = _RULES[park.kind](turn)
    if found is None:
        raise ValueError(f"the replayed turn holds no {park.kind} park")
    return found


def _paused(output: DeferredToolRequests) -> tuple[str, ...]:
    """The calls a pause deferred: any tool but the two that park as themselves."""
    return tuple(
        c.tool_call_id for c in output.calls if output.metadata.get(c.tool_call_id) == PAUSE_METADATA
    )


@dataclass(frozen=True)
class _Turn:
    """The deferred turn, read once for every rule below."""

    output: DeferredToolRequests
    told: int
    paused: tuple[str, ...]
    paused_by: str

    @property
    def connects(self) -> list[ToolCallPart]:
        return [c for c in self.output.calls if c.tool_name == REQUEST_CONNECTOR]

    @property
    def asks(self) -> list[ToolCallPart]:
        return [c for c in self.output.calls if c.tool_name == ASK_USER]

    @property
    def denied(self) -> tuple[str, ...]:
        return tuple(p.tool_call_id for p in self.output.approvals)


def _control(turn: _Turn) -> ControlDeferral | None:
    """**A person at the browser outranks every other ask**: a call that met a live
    control lease, or the agent's own `browser.handoff`, parks the step on ONE CONTROL gate; every
    other pending id of the turn resolves `NOT_RUN_FOR_CONTROL` - except a call a pause deferred,
    which is told it did not run for the pause."""
    if not control_calls(turn.output):
        return None
    control = control_deferral(turn.output)
    return replace(
        control,
        extra_calls=tuple(c for c in control.extra_calls if c not in turn.paused),
        told=turn.told,
        paused_calls=turn.paused,
        paused_by=turn.paused_by,
    )


def _question(turn: _Turn) -> ParkDeferral | None:
    """The first ask_user call, as one batch. The batch shape (`questions: [{question, options}]`),
    tolerating the legacy single-question shape a stale parked run may still carry. A call with no
    readable question does NOT park - it falls through and costs a rung."""
    asks = turn.asks
    if not asks:
        return None
    call = asks[0]
    args = call.args_as_dict()
    raw = args.get("questions")
    if not raw and args.get("question"):
        raw = [{"question": args.get("question"), "options": args.get("options") or []}]
    batch = [
        (str(q.get("question", "")).strip(), [str(o) for o in (q.get("options") or [])])
        for q in (raw or [])
        if isinstance(q, dict) and str(q.get("question", "")).strip()
    ]
    if not batch:
        return None
    return Deferral(
        tool_call_id=call.tool_call_id,
        questions=batch,
        denied_approvals=turn.denied,
        # Connect requests that shared the turn re-request after the answer (the mixed-turn law:
        # one gate at a time, questions first).
        extra_calls=tuple(c.tool_call_id for c in turn.connects),
        # A second ask_user in the same turn: answered with the one-batch string - every pending
        # id must resolve, or pydantic-ai refuses.
        extra_asks=tuple(c.tool_call_id for c in asks[1:]),
        told=turn.told,
        paused_calls=turn.paused,
        paused_by=turn.paused_by,
    )


def _connect(turn: _Turn) -> ParkDeferral | None:
    """The connect block: park on the FIRST request; extras re-request later; approvals
    that shared the turn resolve denied-with-retry, like the ask mixed-turn law."""
    connects = turn.connects
    if not connects:
        return None
    first_args = connects[0].args_as_dict()
    return ConnectDeferral(
        tool_call_id=connects[0].tool_call_id,
        service=str(first_args.get("service", "")).strip(),
        why=str(first_args.get("why", "")).strip(),
        extra_calls=tuple(c.tool_call_id for c in connects[1:]),
        denied_approvals=turn.denied,
        # Asks that could not park (unreadable batch): resolved, never dangled.
        extra_asks=tuple(c.tool_call_id for c in turn.asks),
        told=turn.told,
        paused_calls=turn.paused,
        paused_by=turn.paused_by,
    )


def _approvals(turn: _Turn) -> ParkDeferral | None:
    """Consent alone: side-effectful connector or browser calls under an `ask` policy."""
    if not (turn.output.approvals and not turn.output.calls):
        return None
    return replace(approval_deferral(turn.output), told=turn.told)


def _pause(turn: _Turn) -> ParkDeferral | None:
    """The task is paused: the run stopped at this tool dispatch. Approvals that shared
    the turn are denied on resume; asks that could not park, answered."""
    if not turn.paused:
        return None
    return PauseDeferral(
        calls=turn.paused,
        told=turn.told,
        denied_approvals=turn.denied,
        paused_by=turn.paused_by,
        extra_asks=tuple(c.tool_call_id for c in turn.asks),
    )


# The rule each park kind took, by kind (the budget's reads the turn on its own: `budget_held`).
_RULES: Final[dict[ParkKind, Callable[[_Turn], ParkDeferral | None]]] = {
    ParkKind.CONTROL: _control,
    ParkKind.QUESTION: _question,
    ParkKind.CONNECT: _connect,
    ParkKind.APPROVAL: _approvals,
    ParkKind.PAUSE: _pause,
}


async def status_moved(
    session: AsyncSession, task: Task, status: TaskStatus, *, before: int | None = None
) -> TaskStatusChanged | None:
    """The task's latest status event (below `before`), when it moved the task to `status`."""
    latest = await TaskEvent.latest_of_type(session, task.id, "task.status", before=before)
    return latest if isinstance(latest, TaskStatusChanged) and latest.status is status else None


def mover(event: TaskStatusChanged | None) -> str | None:
    """Who wrote a status event, as another member reads their name."""
    return safe_name(event.actor.name) if event is not None and event.actor is not None else None


async def paused_by(session: AsyncSession, task: Task) -> str:
    """Who paused the task, for the deferred calls' "Paused before this ran"."""
    return mover(await status_moved(session, task, TaskStatus.PAUSED)) or "Someone"
