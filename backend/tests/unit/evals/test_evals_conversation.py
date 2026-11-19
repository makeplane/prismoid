# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The conversation set and its chat runner, offline: every row is one the runner plays, a planted
misroute fails its row, a row missing what it needs fails loudly, and a thread that stops is never scored
as the row's own turn. The API is scripted; no model runs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from evals.evaluators import passes
from evals.run import DATASETS, SUITES, plan
from evals.runners import InfraError, ThreadStalled, TrialTimeout, run_trials
from evals.runners import conversation as chat
from evals.schema import Outcome, OutcomeRow, Surface, Unscored, load_rows
from evals.trace import PrismoidTrace

ROWS = {row.id: row for row in load_rows(DATASETS / "conversation.jsonl")}


@dataclass
class Line:
    """How the scripted chat lane answers one line: the door's verdict, then (for an answer) the reply."""

    door: str = "answer"
    settled: str = "answer"  # the line's verdict once the bot spoke: answer | propose | start
    text: str = "Here it is."
    status: str = "answered"  # answered | failed | (anything else: never answers)
    failure: str = ""


@dataclass
class ChatAPI:
    """A scripted conversations API: one thread, lines answered on the second poll."""

    script: dict[str, Line]
    rows: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    bots: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    sent: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    cancelled: list[str] = field(default_factory=list[str])
    waiting: dict[int, tuple[Line, bool]] = field(default_factory=dict[int, tuple[Line, bool]])
    archived: bool = False
    polls: int = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        if path.startswith("/v1/bots"):
            return self._bot(method, path, body)
        if method == "POST" and path == "/v1/conversations":
            return httpx.Response(200, json={"id": "c1"})
        if method == "POST" and path == "/v1/conversations/c1/messages":
            return self._send(body)
        if method == "GET" and path == "/v1/conversations/c1/messages":
            self.polls += 1
            since = int(request.url.params["since"])
            self._settle()
            return httpx.Response(200, json=[r for r in self.rows if r["seq"] > since])
        if method == "PATCH" and path == "/v1/conversations/c1":
            self.archived = True
            return httpx.Response(200, json={})
        if method == "POST" and path.endswith("/cancel"):
            self.cancelled.append(path.split("/")[3])
            return httpx.Response(202, json={})
        return httpx.Response(404, json={})

    def _bot(self, method: str, path: str, body: dict[str, Any]) -> httpx.Response:
        name = path.removeprefix("/v1/bots").strip("/") or body["name"]
        if method == "GET":
            return httpx.Response(200 if name in self.bots else 404, json=self.bots.get(name, {}))
        self.bots.setdefault(name, {}).update(body)
        return httpx.Response(200, json=self.bots[name])

    def _send(self, body: dict[str, Any]) -> httpx.Response:
        self.sent.append(body)
        line = self.script[body["text"]]
        seq = len(self.rows) + 1
        task = f"t{seq}" if line.door in ("start", "steer", "follow_up") else None
        self.rows.append(
            {"seq": seq, "role": "user", "kind": "text", "text": body["text"], "verdict": line.door}
            | {"status": "sent", "task_id": task, "meta": {}}
        )
        if line.door == "answer":
            self.waiting[seq] = (line, False)
        return httpx.Response(202, json={"seq": seq, "verdict": line.door, "reason": "", "task_id": task})

    def _settle(self) -> None:
        """Answer every waiting line, one poll after it was sent."""
        for seq, (line, seen) in list(self.waiting.items()):
            if not seen:
                self.waiting[seq] = (line, True)
                continue
            del self.waiting[seq]
            row = self.rows[seq - 1]
            if line.status == "failed":
                row["status"] = "failed"
                self._append("system", "failed", line.failure or "could not reply (KeyError)", None)
            elif line.status == "answered":
                row["status"], row["verdict"] = "answered", line.settled
                row["task_id"] = f"t{row['seq']}" if line.settled == "start" else None
                self._append("bot", "text", line.text, None, answered=[row["seq"]])

    def _append(self, role: str, kind: str, text: str, task: str | None, **meta: Any) -> None:
        seq = len(self.rows) + 1
        self.rows.append(
            {"seq": seq, "role": role, "kind": kind, "text": text, "verdict": "none", "status": "sent"}
            | {"task_id": task, "meta": meta}
        )


@pytest.fixture(autouse=True)
def no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat, "POLL_S", 0.0)


async def play(row: OutcomeRow, api: ChatAPI, *, timeout_s: float = 5.0) -> PrismoidTrace:
    async with httpx.AsyncClient(transport=httpx.MockTransport(api), base_url="http://api") as client:
        return await chat.ConversationRunner(client, budget_usd=1.0, timeout_s=timeout_s)(row)


def thread(row: OutcomeRow, last: Line, earlier: Line | None = None) -> dict[str, Line]:
    """The script for a row: its earlier lines answered plainly (or as `earlier` says), its own as `last`."""
    return {text: earlier or Line() for text in row.context.turns} | {row.message: last}


def test_the_set_is_thirty_two_scenarios_and_three_non_english_rows_every_one_playable() -> None:
    rows = list(ROWS.values())
    assert len(rows) == 35
    assert all(row.context.surface is Surface.CONVERSATION and row.scored for row in rows)
    assert all(row.context.turn >= 2 for row in rows), "every scenario is multi-turn"
    assert {row.id: chat.setup_gap(row) for row in rows} == dict.fromkeys(ROWS)
    languages = {row.context.locale.partition("-")[0] for row in rows if row.context.locale}
    assert len(languages - {"en"}) >= 3
    assert sum(row.source == "backend/evals/chat_book.py" for row in rows) >= 32
    assert SUITES["conversation"].chat and SUITES["conversation"].trials == 3


async def test_the_right_ending_passes_and_a_planted_misroute_fails_its_row() -> None:
    row = ROWS["chat:ask-spreadsheet"]
    right = await play(row, ChatAPI(thread(row, Line(settled="propose"))))
    assert (right.outcome, passes(row, right)) == (Outcome.PROPOSE, True)
    planted = await play(row, ChatAPI(thread(row, Line(settled="answer"))))
    assert (planted.outcome, passes(row, planted)) == (Outcome.REPLY, False)


async def test_a_line_that_continues_a_card_is_work_and_the_trial_cancels_what_it_started() -> None:
    row = ROWS["chat:card-shorter"]
    api = ChatAPI(thread(row, Line(door="steer"), earlier=Line(settled="start")))
    trace = await play(row, api)
    assert trace.outcome is Outcome.PLAN and passes(row, trace)
    assert sorted(api.cancelled) == ["t1", "t3"] and api.archived
    talk = await play(row, ChatAPI(thread(row, Line(text="Shorter: ..."), earlier=Line(settled="start"))))
    assert not passes(row, talk), "a reply where the card should have moved is a misroute"


async def test_a_reply_that_hands_the_line_to_the_card_is_work() -> None:
    """The bot's reply may carry the line to the card above (steer, follow-up) or start its waiting
    proposal: the line settles on that verdict, and the person sees the card move."""
    row = ROWS["chat:card-shorter"]
    for settled in ("steer", "follow_up", "start"):
        trace = await play(row, ChatAPI(thread(row, Line(settled=settled), earlier=Line(settled="start"))))
        assert (trace.outcome, passes(row, trace)) == (Outcome.PLAN, True), settled


async def test_the_work_mode_is_the_bots_dial_and_the_gesture_rides_the_rows_line() -> None:
    row = ROWS["chat:gesture-starts"]
    api = ChatAPI(thread(row, Line(door="start")))
    trace = await play(row, api)
    assert trace.outcome is Outcome.PLAN
    assert [s["start_task"] for s in api.sent] == [False, True]
    never = ROWS["chat:never-talks"]
    api = ChatAPI(thread(never, Line()))
    await play(never, api)
    assert api.bots["evals-chat-never"]["work_mode"] == "never"
    assert api.bots["evals-chat-never"]["threads"] == "many"


async def test_a_row_missing_what_the_runner_needs_fails_loudly(tmp_path: Path) -> None:
    good = (DATASETS / "conversation.jsonl").read_text(encoding="utf-8").splitlines()[0]
    unlabelled = json.loads(good)
    del unlabelled["expected_outcome"]
    path = tmp_path / "conversation.jsonl"
    path.write_text(json.dumps(unlabelled) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"(?s)conversation\.jsonl:1: .*expected_outcome"):
        load_rows(path)

    no_surface = json.loads(good)
    del no_surface["context"]["surface"]
    row = OutcomeRow.model_validate(no_surface)
    assert plan([row], None, None, chat.setup_gap)[1] == {row.id: "a task row: the live runner plays it"}
    with pytest.raises(ValueError, match="a task row"):
        await play(row, ChatAPI({}))
    with pytest.raises(ValueError, match="a task row"):  # a harness bug stops the run, never a score
        await run_trials([row], lambda r: play(r, ChatAPI({})), trials=3, concurrency=1)


async def test_a_thread_that_stops_early_is_never_the_rows_miss() -> None:
    row = ROWS["chat:msa-then-sow"]
    with pytest.raises(ThreadStalled):
        await play(row, ChatAPI(thread(row, Line(), earlier=Line(status="failed"))))
    with pytest.raises(InfraError, match="model provider"):
        await play(
            row, ChatAPI(thread(row, Line(status="failed", failure="could not reply (ModelServerError)")))
        )
    unanswered = await play(row, ChatAPI(thread(row, Line(status="failed"))))
    assert unanswered.outcome is None and not passes(row, unanswered)
    with pytest.raises(TrialTimeout):
        await play(row, ChatAPI(thread(row, Line(status="never"))), timeout_s=0.05)


async def test_a_refused_line_is_a_reject_and_scores_three_trials_as_pass_cubed() -> None:
    row = ROWS["chat:card-too-long"]
    refused = await play(row, ChatAPI(thread(row, Line(door="refused"), earlier=Line(settled="start"))))
    assert refused.outcome is Unscored.REJECT
    trials = await run_trials(
        [row],
        lambda r: play(r, ChatAPI(thread(r, Line(door="follow_up"), earlier=Line(settled="start")))),
        trials=3,
        concurrency=1,
    )
    assert [t.trace and passes(row, t.trace) for t in trials] == [True, True, True]
