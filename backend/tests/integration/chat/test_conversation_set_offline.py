# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The conversation set, played offline through the real door and reply lane: every row through the chat
runner over the real API, each reply on the real build with a scripted model that makes the row's
labelled choice. Every row scores as labelled, so what the set measures live is the model's choice
alone; a model that never hands a line to its card fails exactly the card rows."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from evals.evaluators import passes
from evals.run import DATASETS
from evals.runners import conversation as chat_runner
from evals.schema import Outcome, OutcomeRow, Unscored, load_rows
from prismoid.api.app import create_app
from prismoid.api.hub import hub
from prismoid.db.models.conversation import Conversation
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import auth as auth_ops
from prismoid.operations import chat as chat_ops
from prismoid.operations import chat_reply, dispatch, live
from tests.support.chat_model import Choice, model_build

pytestmark = pytest.mark.integration

ROWS = load_rows(DATASETS / "conversation.jsonl")
LATEST = "# The latest message"


class InlineReplies:
    """A dispatcher that answers a chat line on the spot, as the reply job would; other jobs wait."""

    async def enqueue(self, job: str, key: str, **kwargs: Any) -> None:
        if job == "reply_chat":
            cid = uuid.UUID(str(kwargs["conversation_id"]))
            async with row_scope(Conversation, cid) as session:
                await chat_ops.reply(session, cid, int(kwargs["seq"]))

    async def send(self, key: str, payload: Any, topic: str, *, missing_ok: bool = False) -> None:
        return None


def _hands_a_card(row: OutcomeRow) -> bool:
    """The rows whose earlier line leaves a card above the row's own: a started task, or a waiting one."""
    return (row.source_id or "").startswith("card-") or row.id == "chat:yes-on-the-card"


def labelled(row: OutcomeRow) -> Choice:
    """The choice the row's label asks of the bot on its own line."""
    if row.expected_outcome is Outcome.REPLY:
        return None
    if row.expected_outcome is Outcome.PLAN and (row.source_id or "").startswith("card-"):
        return ("continue_card", {})
    if row.expected_outcome is Outcome.PLAN and row.id == "chat:yes-on-the-card":
        return ("accept_pending", {})
    return ("propose_task", {"goal": row.message})


@dataclass
class Oracle:
    """The scripted model: the row's labelled choice on its own line, a proposal on an earlier line
    that must leave a card, a reply otherwise. A tool the reply was not sent is never called."""

    choose: Callable[[OutcomeRow], Choice] = labelled
    row: OutcomeRow | None = None
    sent: list[frozenset[str]] = field(default_factory=list[frozenset[str]])

    def decide(self, brief: str, tools: frozenset[str]) -> Choice:
        assert self.row is not None
        self.sent.append(tools)
        latest = brief.partition(LATEST)[2]
        if self.row.message in latest:
            choice = self.choose(self.row)
        else:
            choice = (
                ("propose_task", {"goal": latest.strip()[:200] or "the work"})
                if _hands_a_card(self.row)
                else None
            )
        return choice if choice is None or choice[0] in tools else None


@pytest.fixture
async def api(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    async def quiet(event: Any) -> None:
        return None

    monkeypatch.setattr(live, "notify", quiet)
    monkeypatch.setattr(chat_runner, "POLL_S", 0.0)
    dispatch.set_dispatcher(InlineReplies())
    async with session_scope() as session:
        signed = await auth_ops.signup(
            session,
            email=f"u{uuid.uuid4().hex[:12]}@example.com",
            name="Ana",
            password="a-long-enough-password",
        )
        await session.commit()
    headers = {"Authorization": f"Bearer {signed.token}"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://test", headers=headers
        ) as client:
            yield client
    finally:
        dispatch.set_dispatcher(dispatch.NullDispatcher())
        await auth_ops.flush_outbound_email()
        await hub.stop()


async def _play(
    api: httpx.AsyncClient, oracle: Oracle, rows: list[OutcomeRow]
) -> dict[str, Outcome | Unscored | None]:
    runner = chat_runner.ConversationRunner(api, budget_usd=1.0, timeout_s=30.0)
    scored: dict[str, Outcome | Unscored | None] = {}
    for row in rows:
        oracle.row = row
        trace = await runner(row)
        scored[row.id] = trace.outcome if passes(row, trace) else None
    return scored


async def test_every_row_scores_as_labelled_through_the_real_lane(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    oracle = Oracle()
    monkeypatch.setattr(chat_reply, "build", model_build(oracle.decide))
    scored = await _play(api, oracle, ROWS)
    assert scored == {row.id: row.expected_outcome for row in ROWS}
    assert any("continue_card" in tools for tools in oracle.sent), "a card was above some line"


async def test_a_model_that_never_hands_a_line_to_its_card_fails_the_card_rows(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_card(row: OutcomeRow) -> Choice:
        choice = labelled(row)
        return None if choice is not None and choice[0] == "continue_card" else choice

    oracle = Oracle(choose=no_card)
    monkeypatch.setattr(chat_reply, "build", model_build(oracle.decide))
    rows = [
        r for r in ROWS if r.id in ("chat:card-shorter", "chat:card-question-is-talk", "chat:yes-on-the-card")
    ]
    scored = await _play(api, oracle, rows)
    assert scored == {
        "chat:card-shorter": None,
        "chat:card-question-is-talk": Outcome.REPLY,
        "chat:yes-on-the-card": Outcome.PLAN,
    }
