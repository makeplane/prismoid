# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The chat runner: a conversation row played as a real bot conversation, one line at a time.

Each trial opens a fresh conversation with a draft bot the runner keeps per work mode
(`evals-chat-auto`, `-ask`, `-never`; made on first use, a chat per topic, so no two trials share a
thread) and sends the row's earlier user lines, then its own, through the conversation door. A line
ends on what the person sees next, and that is its outcome:

    plan     the door started work, or continued a task's card (start, steer, follow_up), or the
             bot's reply did: its proposal started under the owner's standing grant, it accepted
             its waiting proposal on the person's yes, or it handed the line to the card above
    propose  the bot replied and its proposal waits on the person
    reply    the bot replied and nothing waits
    reject   the door refused the line
    None     the bot never answered (its reply failed), or the clock ran out

A started task is scored `plan` the moment it starts: this suite measures the conversation's decision,
and what the task then made is the task suites' question. The API carries neither a reply's tool calls
nor its spend, so a reply is never told from light work here and cost reads zero.

An earlier line with no ending is `ThreadStalled`; the row's own line past its clock is a
`TrialTimeout`; a reply the model provider failed, a 5xx, a 429 or a dropped connection is an
`InfraError`; any other 4xx is a harness bug and stops the run. Every task a trial started is
cancelled and its conversation archived when the trial ends. Live only in the final pass; offline it is
driven against a scripted API.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, cast

import httpx

from evals.runners import InfraError, ThreadStalled, TrialTimeout
from evals.runners.live import PROVIDER_FAILURE_NAMES, earlier_turns
from evals.schema import Outcome, OutcomeRow, Surface, Unscored
from evals.trace import PrismoidTrace
from prismoid.schemas.conversation import Verdict, WorkMode

POLL_S = 1.0
BOT_PREFIX = "evals-chat-"
BOT_INSTRUCTIONS = "You help the person who owns you with whatever they bring to the conversation."

# The verdicts the door acts on at once: the line's ending is the task card, not a reply.
WORK_VERDICTS = frozenset({Verdict.START, Verdict.STEER, Verdict.FOLLOW_UP})
# The settled verdict of an answered line, and what the person got: the bot's reply may start its
# proposal, or hand the line to the card above it.
REPLY_OUTCOMES: dict[Verdict, Outcome] = {
    Verdict.ANSWER: Outcome.REPLY,
    Verdict.PROPOSE: Outcome.PROPOSE,
    Verdict.START: Outcome.PLAN,
    Verdict.STEER: Outcome.PLAN,
    Verdict.FOLLOW_UP: Outcome.PLAN,
}
# The setup this runner sends: the bot's work mode (a dial) and the start gesture on the row's line.
SENT_DIALS = frozenset({"work_mode"})
SENT_FACTS = frozenset({"gesture"})


def setup_gap(row: OutcomeRow, connected: frozenset[str] | None = None) -> str | None:
    """Why this runner cannot play the row, or None. `connected` is unused: a chat line attaches none."""
    context, earlier = row.context, earlier_turns(row)
    gaps = (
        (not row.scored, f"a {row.expected_outcome.value} row is never scored"),
        (context.surface is not Surface.CONVERSATION, "a task row: the live runner plays it"),
        (
            bool(set(context.dials) - SENT_DIALS),
            f"dials the chat runner does not set: {sorted(context.dials)}",
        ),
        (bool(set(context.facts) - SENT_FACTS), f"facts the chat runner cannot set: {sorted(context.facts)}"),
        (context.dials.get("work_mode", WorkMode.AUTO) not in set(WorkMode), "an unknown work mode"),
        (bool(context.inputs or context.connectors), "a chat line carries no attachment or connector"),
        (context.prior_host is not None or context.transact, "needs task-side state a chat cannot set"),
        (context.deployment is not None or context.deployment_reach is not None, "a deployment condition"),
        (context.turn != len(earlier) + 1, f"says turn {context.turn} but supplies {len(earlier)} earlier"),
    )
    return next((why for blocked, why in gaps if blocked), None)


@dataclass(frozen=True)
class _Line:
    """One line's ending, as the thread shows it."""

    outcome: Outcome | Unscored | None
    text: str = ""
    task_id: str | None = None
    latency_s: float | None = None


class ConversationRunner:
    """Plays conversation rows on one API connection. Share it across concurrent trials."""

    def __init__(self, client: httpx.AsyncClient, *, budget_usd: float, timeout_s: float) -> None:
        self._client = client
        self._budget_usd = budget_usd
        self._timeout_s = timeout_s
        self._bots: set[str] = set()
        self._making = asyncio.Lock()

    async def __call__(self, row: OutcomeRow, /) -> PrismoidTrace:
        if gap := setup_gap(row):
            raise ValueError(f"{row.id}: {gap}")
        bot = await self._bot(WorkMode(row.context.dials.get("work_mode", WorkMode.AUTO)))
        opened = await self._send_json("POST", "/v1/conversations", {"bot": bot})
        conversation = str(opened["id"])
        started: list[str] = []
        try:
            return await self._play(row, conversation, started)
        finally:
            await asyncio.shield(self._close(conversation, started))

    async def _play(self, row: OutcomeRow, conversation: str, started: list[str]) -> PrismoidTrace:
        earlier = earlier_turns(row)
        prior = ""
        for number, text in enumerate(earlier, start=1):
            try:
                line = await self._line(conversation, text, gesture=False)
            except TimeoutError:
                line = _Line(None)
            started.extend([line.task_id] if line.task_id else [])
            if line.outcome is None or line.outcome is Unscored.REJECT:
                raise ThreadStalled(f"line {number} of the thread had no ending; line {number + 1} never ran")
            prior = line.text
        try:
            line = await self._line(conversation, row.message, gesture=bool(row.context.facts.get("gesture")))
        except TimeoutError:
            raise TrialTimeout(PrismoidTrace(prior_text=prior)) from None
        started.extend([line.task_id] if line.task_id else [])
        if line.outcome is Unscored.REJECT:
            return PrismoidTrace.refused()
        return PrismoidTrace(
            outcome=line.outcome,
            delivered=line.outcome is not None,
            final_text=line.text,
            prior_text=prior,
            latency_s=line.latency_s,
        )

    async def _line(self, conversation: str, text: str, *, gesture: bool) -> _Line:
        """Send one line and wait for its ending; TimeoutError when the bot outlives the clock."""
        began = time.monotonic()
        path = f"/v1/conversations/{conversation}/messages"
        response = await self._request("POST", path, {"text": text, "start_task": gesture})
        if response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY:
            return _Line(Unscored.REJECT)
        sent = cast("dict[str, Any]", response.json())
        verdict, task = Verdict(sent["verdict"]), sent.get("task_id")
        if verdict is Verdict.REFUSED:
            return _Line(Unscored.REJECT)
        if verdict in WORK_VERDICTS:
            return _Line(Outcome.PLAN, task_id=task and str(task), latency_s=time.monotonic() - began)
        async with asyncio.timeout(self._timeout_s):
            line = await self._answered(conversation, int(sent["seq"]))
        return _Line(line.outcome, line.text, line.task_id, time.monotonic() - began)

    async def _answered(self, conversation: str, seq: int) -> _Line:
        """Poll the thread until the bot has answered line `seq` or failed to."""
        while True:
            if (line := await self._settled(conversation, seq)) is not None:
                return line
            await asyncio.sleep(POLL_S)

    async def _settled(self, conversation: str, seq: int) -> _Line | None:
        """The line's ending once the bot has answered it or failed to; None while it is pending."""
        rows = cast(
            "list[dict[str, Any]]",
            await self._send_json(
                "GET", f"/v1/conversations/{conversation}/messages", params={"since": seq - 1}
            ),
        )
        mine = next(m for m in rows if m["seq"] == seq)
        if mine["status"] == "failed":
            failure = next((m["text"] for m in rows if m["kind"] == "failed" and m["task_id"] is None), "")
            if any(f"({name})" in failure for name in PROVIDER_FAILURE_NAMES):
                raise InfraError(f"the model provider failed the reply to line {seq}: {failure[:200]}")
            return _Line(None)
        if mine["status"] != "answered":
            return None
        reply = next(m for m in rows if m["role"] == "bot" and seq in (m["meta"].get("answered") or ()))
        task = mine["task_id"]
        return _Line(REPLY_OUTCOMES[Verdict(mine["verdict"])], reply["text"], task and str(task))

    async def _bot(self, mode: WorkMode) -> str:
        """The bot this work mode talks through, made once per run: a draft, a chat per topic."""
        name = f"{BOT_PREFIX}{mode.value}"
        async with self._making:
            if name not in self._bots:
                found = await self._request("GET", f"/v1/bots/{name}")
                if found.status_code == HTTPStatus.NOT_FOUND:
                    await self._send_json(
                        "POST", "/v1/bots", {"name": name, "instructions": BOT_INSTRUCTIONS}
                    )
                else:
                    found.raise_for_status()
                face = {"work_mode": mode.value, "threads": "many", "task_budget_usd": self._budget_usd}
                await self._send_json("PATCH", f"/v1/bots/{name}", face)
                self._bots.add(name)
        return name

    async def _close(self, conversation: str, started: list[str]) -> None:
        """Stop what the trial started and archive its thread. Best effort: a task may have just ended."""
        with suppress(httpx.HTTPError):
            for task in started:
                await self._client.post(f"/v1/tasks/{task}/cancel")
            await self._client.patch(f"/v1/conversations/{conversation}", json={"archived": True})

    async def _send_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        params: dict[str, int] | None = None,
    ) -> Any:
        """A request whose answer the harness needs: a 422 here is a harness bug, like any other 4xx."""
        response = await self._request(method, path, body, params=params)
        response.raise_for_status()
        return response.json()

    async def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        params: dict[str, int] | None = None,
    ) -> httpx.Response:
        """One request; the stack failing is infra, and a 4xx other than 404 and 422 stops the run."""
        try:
            response = await self._client.request(method, path, json=body, params=params)
        except httpx.TransportError as error:
            raise InfraError(f"{method} {path}: {error!r}") from error
        failed = response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR
        if failed or response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            raise InfraError(f"{method} {path}: HTTP {response.status_code}")
        if response.status_code not in (HTTPStatus.NOT_FOUND, HTTPStatus.UNPROCESSABLE_ENTITY):
            response.raise_for_status()
        return response
