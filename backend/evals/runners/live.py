# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The live runner: a row as a real task through the API, one turn at a time, in one process.

TTFT runs from the client's submit (read just before the POST) to the first `message.delta` frame.
Deltas are never persisted or replayed and the API cannot create a task before it starts, so the
stream can only open after the POST answers: the runner counts a delta only when it also saw that
message's `message.start`, and a turn whose text began before the stream opened has no TTFT rather
than a later delta's. Everything else (cost, calls, tools, Machines, gates, the outcome) is read from
the durable record by `PrismoidTrace.from_events`, never from the runner's own timing.

A turn ends on the first of: the task stopping (delivered, failed, cancelled), a gate opening (the
turn needs a person, and nobody here answers), or its clock. A turn that did not deliver is cancelled
so it stops spending. After a delivery the runner waits a short while only for what a row checks that
lands later (a memory write); the verdict, the title and their spend are never waited for and never
counted in `$/task`.

A row that needs something this runner cannot set up (a thread it does not carry, a deployment
condition, a connector the user has not connected) is not run: `setup_gap` says why, and the suite
reports it as not run instead of scoring a different question.

Failures: the stack, the connection or the model provider failing is an `InfraError` (retried once
by `run_trials`); an earlier turn of the thread that asked, failed or never answered is a
`ThreadStalled` (the agent's doing, never retried); the API refusing the ask (422) is a `reject`
trace; a turn that outlives its clock is a `TrialTimeout` carrying what it had done; any other 4xx
means the harness is wrong (a bad token, a missing route) and stops the run. Whatever ends a trial
early, the run being cancelled included, the tasks it created are cancelled so none keeps spending.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, cast

import httpx
from pydantic_ai import exceptions as model_errors

from evals.runners import InfraError, ThreadStalled, TrialTimeout
from evals.schema import CheckKind, OutcomeRow, Surface
from evals.trace import PrismoidTrace, events_from_payloads
from prismoid.runner import stream
from prismoid.schemas.connectors import ConnectorStatus

AFTERMATH_S = 30.0  # how long past a delivery to wait for an event a row's check reads
PAGE = 200  # the API's largest page

# What lands after `task.delivered`, and the check that reads it.
LATE_EVENTS: dict[CheckKind, str] = {CheckKind.MEMORY_WRITTEN: "memory.written"}

# The dials a row can set that this runner sends; any other dial is a gap.
SENT_DIALS = frozenset({"deliverable", "mode"})

CHAT_ROW = "conversation rows need the chat lane's runner (--suite conversation)"

# The provider's failures, by the class name a failed turn's reason opens with (`Name: message`).
PROVIDER_FAILURES: frozenset[type[BaseException]] = frozenset(
    {
        model_errors.ModelRateLimitError,
        model_errors.ModelQuotaExceededError,
        model_errors.ModelUnavailableError,
        model_errors.ModelServerError,
        model_errors.ModelConnectionError,
        model_errors.ModelRequestTimeout,
        model_errors.FallbackExceptionGroup,
    }
)


def _with_subclasses(kinds: frozenset[type[BaseException]]) -> frozenset[str]:
    names: set[str] = set()
    pending = list(kinds)
    while pending:
        kind = pending.pop()
        names.add(kind.__name__)
        pending.extend(kind.__subclasses__())
    return frozenset(names)


PROVIDER_FAILURE_NAMES = _with_subclasses(PROVIDER_FAILURES)


def earlier_turns(row: OutcomeRow) -> tuple[str, ...]:
    """The user turns played before the row's message; the assistant's side is whatever answers live."""
    return (*row.context.turns, *(t.text for t in row.prior_turns if t.role == "user"))


def setup_gap(row: OutcomeRow, connected: frozenset[str] | None) -> str | None:
    """Why this runner cannot run the row, or None; `connected` None (a dry run) asks nothing of connectors."""
    context, earlier = row.context, earlier_turns(row)
    unsent = sorted(set(context.dials) - SENT_DIALS)
    missing = [] if connected is None else sorted(set(context.connectors) - connected)
    gaps = (
        (not row.scored, f"a {row.expected_outcome.value} row is never scored"),
        (context.surface is not Surface.TASK, CHAT_ROW),
        (bool(context.facts), "facts describe a state the live runner cannot set"),
        (context.prior_host is not None, "needs a thread whose last work drove a site"),
        (context.transact, "needs the transact dial"),
        (context.deployment is not None, f"deployment: {context.deployment}"),
        (
            context.deployment_reach is not None,
            f"needs a deployment whose reach is {context.deployment_reach}",
        ),
        (bool(context.inputs), "attachments are not sent by the live runner"),
        (context.turn > len(earlier) + 1, f"says turn {context.turn} but supplies {len(earlier)} earlier"),
        (bool(unsent), f"dials the live runner does not send: {', '.join(unsent)}"),
        (
            bool(earlier) and bool(context.dials or context.connectors),
            "dials and connectors ride the first turn; this row sets them on a later one",
        ),
        (bool(missing), f"connectors not connected for this user: {', '.join(missing)}"),
    )
    return next((why for blocked, why in gaps if blocked), None)


async def connected_slugs(client: httpx.AsyncClient) -> frozenset[str]:
    """The slugs of the connectors this user has connected and working."""
    slugs: set[str] = set()
    cursor: str | None = None
    while True:
        params: dict[str, str | int] = {"limit": PAGE}
        if cursor:
            params["cursor"] = cursor
        listed = await client.get("/v1/connectors", params=params)
        listed.raise_for_status()
        page = cast("dict[str, Any]", listed.json())
        slugs.update(c["slug"] for c in page["items"] if c["status"] == ConnectorStatus.LIVE)
        cursor = page.get("next_cursor")
        if not cursor:
            return frozenset(slugs)


def _setup(row: OutcomeRow) -> dict[str, Any]:
    """What the row sets on the task it creates: its deliverable and mode dials and its connectors."""
    context = row.context
    setup: dict[str, Any] = {}
    if deliverable := context.dials.get("deliverable"):
        setup["deliverable"] = deliverable
    if mode := context.dials.get("mode"):
        setup["mode"] = mode
    if context.connectors:
        setup["connectors"] = list(context.connectors)
    return setup


def _awaited(row: OutcomeRow) -> set[str]:
    """The events a row's checks read that arrive after delivery."""
    return {LATE_EVENTS[check.type] for check in row.checks if check.type in LATE_EVENTS}


def _frame(line: str) -> dict[str, Any] | None:
    """The JSON of an SSE `data:` line; heartbeats, comments and field lines are not frames."""
    if not line.startswith("data:"):
        return None
    try:
        decoded = json.loads(line.removeprefix("data:"))
    except ValueError:
        return None
    return cast("dict[str, Any]", decoded) if isinstance(decoded, dict) else None


@dataclass
class _Turn:
    """What one turn's stream has shown so far. The stream replays the whole record, so frames
    before `head` are earlier turns: kept for the trace, deciding nothing."""

    task_id: str
    head: int
    submitted_at: dt.datetime
    awaiting: set[str]
    payloads: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    ttft_s: float | None = None
    started: set[str] = field(default_factory=set[str])  # channels whose `message.start` arrived
    texted: bool = False  # a delta of this turn arrived, seen from its start or not
    delivered: bool = False
    stopped: bool = False  # the task is over, or a gate is waiting on a person

    @property
    def begun(self) -> bool:
        """Whether any frame of this turn itself has arrived."""
        return any(frame["seq"] >= self.head for frame in self.payloads)

    def take(self, frame: dict[str, Any], arrived_at: dt.datetime) -> bool:
        """Take one frame in; True when the turn is over."""
        if frame.get("lane") == "live":
            if frame.get("type") == "message.start":
                self.started.add(str(frame.get("channel")))
            elif frame.get("type") == "message.delta" and not self.texted:
                self.texted = True
                if frame.get("channel") in self.started:
                    self.ttft_s = (arrived_at - self.submitted_at).total_seconds()
            return False
        self.payloads.append(frame)
        if frame["seq"] < self.head:
            return False
        kind = frame["type"]
        self.awaiting.discard(kind)
        self.delivered = self.delivered or kind == "task.delivered"
        if self.delivered:
            return not self.awaiting
        self.stopped = stream.task_stopped(frame) or kind == "gate.opened"
        return self.stopped

    def provider_failure(self) -> str | None:
        """The reason this turn failed, when the model provider failed it; None otherwise."""
        for frame in self.payloads:
            if frame["seq"] >= self.head and frame["type"] == "task.status" and frame["status"] == "failed":
                reason = str(frame.get("reason", ""))
                if reason.partition(":")[0] in PROVIDER_FAILURE_NAMES:
                    return reason
        return None


class LiveRunner:
    """Runs rows as real tasks on one API connection. Share it across concurrent trials."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        budget_usd: float,
        timeout_s: float,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self._client = client
        self._budget_usd = budget_usd
        self._timeout_s = timeout_s
        self._now = now

    async def __call__(self, row: OutcomeRow, /) -> PrismoidTrace:
        created: list[str] = []
        try:
            turn = await self._play_thread(row, created)
        except BaseException:
            await asyncio.shield(self._cancel_all(created))
            raise
        if turn is None:
            return PrismoidTrace.refused()
        earlier = earlier_turns(row)
        trace = PrismoidTrace.from_events(
            events_from_payloads(turn.payloads),
            turn=len(earlier) + 1,
            ttft_s=turn.ttft_s,
            submitted_at=turn.submitted_at,
        )
        if turn.delivered:
            return trace
        await self._cancel(turn.task_id)
        if turn.stopped:
            return trace
        raise TrialTimeout(trace)

    async def _play_thread(self, row: OutcomeRow, created: list[str]) -> _Turn | None:
        """Play the earlier turns, then the row's own; None when the API refused one. A task this
        creates is put in `created` before anything else can fail."""
        task_id: str | None = None
        earlier = earlier_turns(row)
        for number, message in enumerate((*earlier, row.message), start=1):
            last = number > len(earlier)
            turn = await self._submit(
                task_id, message, _setup(row), awaiting=_awaited(row) if last else set()
            )
            if turn is None:
                return None
            if task_id is None:
                created.append(turn.task_id)
            task_id = turn.task_id
            await self._follow(turn)
            if failure := turn.provider_failure():
                raise InfraError(f"the model provider failed turn {number} of {task_id}: {failure[:200]}")
            if last:
                return turn
            if not turn.delivered:
                raise ThreadStalled(
                    f"turn {number} of the thread did not deliver; turn {number + 1} never ran"
                )
        raise AssertionError("unreachable: the row's own turn always ends the loop")

    async def _submit(
        self, task_id: str | None, message: str, setup: dict[str, Any], *, awaiting: set[str]
    ) -> _Turn | None:
        """Submit one turn: a new task carrying the row's setup, or the next turn of `task_id`."""
        body: dict[str, Any] = {"goal": message, "budget": {"usd": self._budget_usd}}
        if task_id is None:
            body |= setup
        submitted_at = self._now()
        accepted = await self._post(task_id, body)
        if accepted.status_code == HTTPStatus.UNPROCESSABLE_ENTITY:
            return None
        reply = cast("dict[str, Any]", accepted.json())
        return _Turn(str(reply["task_id"]), int(reply["last_seq"]), submitted_at, awaiting)

    async def _post(self, task_id: str | None, body: dict[str, Any]) -> httpx.Response:
        """A new task, or the next turn of `task_id`."""
        path = "/v1/tasks" if task_id is None else f"/v1/tasks/{task_id}/follow_up"
        try:
            response = await self._client.post(path, json=body)
        except httpx.TransportError as error:
            raise InfraError(f"POST {path}: {error!r}") from error
        failed = response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR
        if failed or response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            raise InfraError(f"POST {path}: HTTP {response.status_code}")
        if response.status_code != HTTPStatus.UNPROCESSABLE_ENTITY:
            response.raise_for_status()
        return response

    async def _follow(self, turn: _Turn) -> None:
        """Read the stream until the turn ends or its clock runs out; a dead stream, or no frame at all, is infra."""
        try:
            async with asyncio.timeout(self._timeout_s) as clock:
                await self._read(turn, clock)
        except httpx.TransportError as error:
            raise InfraError(f"events of {turn.task_id}: {error!r}") from error
        except TimeoutError:
            if not turn.begun:
                raise InfraError(f"nothing of turn {turn.head} arrived for {turn.task_id} in time") from None

    async def _read(self, turn: _Turn, clock: asyncio.Timeout) -> None:
        async with self._client.stream(
            "GET", f"/v1/tasks/{turn.task_id}/events", params={"since": 0}
        ) as response:
            if response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
                raise InfraError(f"events of {turn.task_id}: HTTP {response.status_code}")
            response.raise_for_status()
            async for line in response.aiter_lines():
                frame = _frame(line)
                if frame is None:
                    continue
                delivering = not turn.delivered
                if turn.take(frame, self._now()):
                    return
                if delivering and turn.delivered:  # only the late events are left to wait for
                    clock.reschedule(asyncio.get_running_loop().time() + AFTERMATH_S)
        raise InfraError(f"the event stream of {turn.task_id} closed before the turn ended")

    async def _cancel_all(self, task_ids: list[str]) -> None:
        """Stop every task this trial created; shielded by the caller, so a cancelled run still stops them."""
        for task_id in task_ids:
            await self._cancel(task_id)

    async def _cancel(self, task_id: str) -> None:
        """Stop a turn that did not deliver, so it stops spending. Best effort: it may have just ended."""
        with suppress(httpx.HTTPError):
            await self._client.post(f"/v1/tasks/{task_id}/cancel")
