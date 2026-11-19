# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid gates resolve --scope` and the inline decision's scope word-matching: a browser allowlist
gate's accept carries a scope only when one is named (the server
picks the default); answers read the ONE gate they resolve (`GET /v1/gates/{id}`), so a Project
editor answers a teammate's task the way its creator does."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import typer

from prismoid.runner import questions as runner_questions
from prismoid.runner.cli import gates as gates_cli
from prismoid.runner.cli import tasks as tasks_cli
from prismoid.runner.cli.choices import ApproveScope


def _calls(monkeypatch: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace `client.request` with a recorder; `GET /v1/gates/{id}` answers from `rows` (an
    absent id fails the way the client fails a 404), everything else records a stub."""
    seen: list[dict[str, Any]] = []

    def fake_request(method: str, path: str, *, body: Any = None, as_json: bool = False, **kw: Any) -> Any:
        seen.append({"method": method, "path": path, "body": body})
        if method == "GET" and path.startswith("/v1/gates/"):
            gate_id = path.removeprefix("/v1/gates/")
            row = next((r for r in rows if r["id"] == gate_id), None)
            if row is None:
                raise typer.Exit(1)  # `client.fail` on the 404 "no open gate"
            return row
        return {"ok": True}

    monkeypatch.setattr(gates_cli.client, "request", fake_request)
    return seen


def test_accept_with_explicit_scope_sends_it(monkeypatch: Any) -> None:
    seen = _calls(monkeypatch, [])
    gates_cli.gates_resolve(
        gate_id="g1",
        answers="",
        accept=True,
        scope=ApproveScope.ONCE,
        decline=False,
        cancel=False,
        done=False,
        json_out=True,
    )
    assert seen == [
        {
            "method": "POST",
            "path": "/v1/gates/g1/resolve",
            "body": {"action": "accept", "content": {"scope": "once"}},
        }
    ]


@pytest.mark.parametrize(
    "scopes",
    [["once", "task", "always"], ["once", "always"], []],
    ids=["offers-task", "first-offer", "offers-none"],
)
def test_accept_with_no_scope_leaves_the_default_to_the_server(monkeypatch: Any, scopes: list[str]) -> None:
    """No lookup: `GET /v1/gates` is the creator's own queue, so a Project editor's bare
    `--accept` once found no gate, sent no scope and came back 422. The server picks the
    gate's default scope (`task`, else its first offer, or none for a gate that offers none)
    for every client."""
    seen = _calls(monkeypatch, [{"id": "g1", "title": "t", "scopes": scopes}])
    gates_cli.gates_resolve(
        gate_id="g1",
        answers="",
        accept=True,
        scope=None,
        decline=False,
        cancel=False,
        done=False,
        json_out=True,
    )
    assert seen == [{"method": "POST", "path": "/v1/gates/g1/resolve", "body": {"action": "accept"}}]


def test_done_hands_a_device_browser_back_as_a_plain_accept(monkeypatch: Any) -> None:
    """`--done`: the creator's accept on a device task's hand-off - no
    scope, no lookup; the server says whether this gate takes it."""
    seen = _calls(monkeypatch, [])
    gates_cli.gates_resolve(
        gate_id="g1",
        answers="",
        accept=False,
        scope=None,
        decline=False,
        cancel=False,
        done=True,
        json_out=True,
    )
    assert seen == [{"method": "POST", "path": "/v1/gates/g1/resolve", "body": {"action": "accept"}}]


def test_a_gate_that_is_not_open_fails_before_any_resolve(monkeypatch: Any) -> None:
    seen = _calls(monkeypatch, [])  # the read 404s: answered, expired, or not yours to see
    with pytest.raises(typer.Exit):
        gates_cli.gates_resolve(
            gate_id="ghost",
            answers="1: eur",
            accept=False,
            scope=None,
            decline=False,
            cancel=False,
            done=False,
            json_out=True,
        )
    assert [c["method"] for c in seen] == ["GET"]


def test_answers_resolve_a_gate_read_by_its_id(monkeypatch: Any) -> None:
    """The editor's path: the gate is not in their own queue (`GET /v1/gates` is the creator's),
    but it reads by id, and the numbered answers map onto its questions."""
    questions = {
        "title": "One question",
        "questions": [{"id": "q1", "prompt": "Which currency?", "type": "single_select"}],
    }
    seen = _calls(monkeypatch, [{"id": "g1", "title": "One question", "questions": questions}])
    gates_cli.gates_resolve(
        gate_id="g1",
        answers="1: eur",
        accept=False,
        scope=None,
        decline=False,
        cancel=False,
        done=False,
        json_out=True,
    )
    assert seen == [
        {"method": "GET", "path": "/v1/gates/g1", "body": None},
        {
            "method": "POST",
            "path": "/v1/gates/g1/resolve",
            "body": {"action": "accept", "content": {"q1": "eur"}},
        },
    ]


def test_fork_posts_a_body(monkeypatch: Any) -> None:
    """`POST /v1/tasks/{id}/fork` takes a `ForkIn` body: a request with none is a 422."""
    seen: list[dict[str, Any]] = []

    def fake_request(method: str, path: str, *, body: Any = None, as_json: bool = False, **kw: Any) -> Any:
        seen.append({"method": method, "path": path, "body": body})
        return {"task_id": "child"}

    monkeypatch.setattr(gates_cli.client, "request", fake_request)
    tasks_cli.tasks_fork(task_id="t1", folder=None, json_out=True)
    assert seen == [{"method": "POST", "path": "/v1/tasks/t1/fork", "body": {}}]


class TestMatchScope:
    def test_the_scopes_own_name_or_first_letter_picks_it(self) -> None:
        scopes = ["once", "task", "always"]
        assert runner_questions.match_scope("once", scopes) == "once"
        assert runner_questions.match_scope("o", scopes) == "once"
        assert runner_questions.match_scope("Always", scopes) == "always"
        # A standing allowance is never one keystroke away (whole-branch review).
        assert runner_questions.match_scope("a", scopes) is None
        assert runner_questions.match_scope("t", scopes) == "task"

    def test_a_bare_allow_picks_task_when_offered(self) -> None:
        assert runner_questions.match_scope("allow", ["once", "task", "always"]) == "task"
        assert runner_questions.match_scope("yes", ["once", "task", "always"]) == "task"

    def test_a_bare_allow_falls_back_to_the_first_offer_when_task_is_not_one(self) -> None:
        assert runner_questions.match_scope("allow", ["once", "always"]) == "once"

    def test_no_scopes_offered_or_no_match_is_none(self) -> None:
        assert runner_questions.match_scope("allow", []) is None
        assert runner_questions.match_scope("banana", ["once", "task", "always"]) is None


# ── prismoid do --wait meets "Budget reached" ────────────────────────────────────────────────────
# Declining a budget gate CANCELS the task (`run_task._hold_for_budget`), so the decision prompt's
# "skip proceeds without it" was a lie that killed the task. A budget gate offers resume or cancel;
# Enter leaves it waiting.


def _replies(monkeypatch: Any, *replies: str) -> list[tuple[str, str, Any]]:
    sent: list[tuple[str, str, Any]] = []
    queue = list(replies)

    def fake_request(method: str, path: str, **kw: Any) -> Any:
        sent.append((method, path, kw.get("body")))
        return {"action": (kw.get("body") or {}).get("action")}

    monkeypatch.setattr(runner_questions.client, "request", fake_request)
    monkeypatch.setattr(runner_questions.typer, "prompt", lambda *a, **kw: queue.pop(0))
    return sent


def _budget(**kw: Any) -> None:
    runner_questions.ask_budget(
        "g1",
        title="Budget reached",
        reason="the task budget of $1.00 is spent ($1.20 so far); raise it and resume, or cancel",
        scope=kw.get("scope", "task"),
        task_id="t1",
        as_json=False,
    )


@pytest.mark.parametrize("reply", ["", "skip", "no"])
def test_a_budget_gate_left_unanswered_stays_waiting_and_never_says_proceeding(
    monkeypatch: Any, capsys: Any, reply: str
) -> None:
    sent = _replies(monkeypatch, reply)
    _budget()
    out = capsys.readouterr().out
    assert sent == [], "no decline - declining a budget gate cancels the task"
    assert "proceeding" not in out and "left waiting" in out
    assert "prismoid tasks budget t1 --usd" in out  # how to raise it and resume
    assert "cancel" in out and "stops the task" in out


def test_a_budget_gate_resumes_or_cancels(monkeypatch: Any, capsys: Any) -> None:
    sent = _replies(monkeypatch, "resume")
    _budget()
    assert sent == [("POST", "/v1/gates/g1/resolve", {"action": "accept"})]
    sent = _replies(monkeypatch, "cancel")
    _budget()
    assert sent == [("POST", "/v1/gates/g1/resolve", {"action": "cancel"})]


def test_a_refused_resume_says_why_and_asks_again(monkeypatch: Any, capsys: Any) -> None:
    sent: list[Any] = []
    queue = ["resume", ""]

    def fake_request(method: str, path: str, **kw: Any) -> Any:
        sent.append(kw.get("body"))
        if kw.get("raise_refused"):
            raise runner_questions.client.Refused("conflict: The Project's monthly budget is still spent", 1)
        raise AssertionError("a refused resume must come back to the prompt, not end the command")

    monkeypatch.setattr(runner_questions.client, "request", fake_request)
    monkeypatch.setattr(runner_questions.typer, "prompt", lambda *a, **kw: queue.pop(0))
    _budget(scope="project")
    out = capsys.readouterr().out
    assert sent == [{"action": "accept"}] and "still spent" in out and "left waiting" in out
    assert "Project's owner" in out and "prismoid tasks budget" not in out  # an editor cannot raise it


def test_the_stream_hands_a_budget_gate_to_the_budget_prompt(monkeypatch: Any) -> None:
    from prismoid.runner import stream

    opened = {
        "type": "gate.opened",
        "gate_id": "g1",
        "task_id": "t1",
        "kind": "budget",
        "title": "Budget reached",
        "reason": "the project budget of $5 is spent ($9 so far); raise it and resume, or cancel",
        "budget_scope": "project",
        "questions": None,
    }
    lines = ["event: gate.opened", f"data: {json.dumps(opened)}"]
    monkeypatch.setattr(stream.client, "stream_sse", lambda *a, **kw: iter(lines))
    monkeypatch.setattr(stream.questions, "interactive", lambda: True)
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        stream.questions, "ask_budget", lambda gate_id, **kw: seen.append({"gate": gate_id, **kw})
    )

    def never(*a: Any, **kw: Any) -> None:
        raise AssertionError("a budget gate is not an approve/connect decision")

    monkeypatch.setattr(stream.questions, "ask_decision", never)
    stream.follow_events("t1", follow=False, json_out=False, answer=True)
    assert seen == [
        {
            "gate": "g1",
            "title": "Budget reached",
            "reason": opened["reason"],
            "scope": "project",
            "task_id": "t1",
            "as_json": False,
        }
    ]


@pytest.mark.parametrize(
    ("flags", "said"),
    [
        ({"accept": True, "scope": ApproveScope.ONCE}, "gate accepted"),
        ({"done": True}, "gate accepted"),
        ({"decline": True}, "gate declined"),
        ({"cancel": True}, "gate cancelled"),
    ],
)
def test_the_terminal_says_what_happened_in_words(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str], flags: dict[str, Any], said: str
) -> None:
    _calls(monkeypatch, [])
    args: dict[str, Any] = {
        "answers": "",
        "accept": False,
        "scope": None,
        "decline": False,
        "cancel": False,
        "done": False,
    }
    gates_cli.gates_resolve(gate_id="g1", json_out=False, **{**args, **flags})
    assert capsys.readouterr().out.strip() == said


# ── a device task's hand-off: Done ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("reply", "action", "said"),
    [
        ("done", "accept", "handed back"),
        ("skip", "decline", "going on without it"),
        ("cancel", "cancel", "cancelled"),
    ],
)
def test_a_device_hand_off_is_done_skip_or_cancel(
    monkeypatch: Any, capsys: Any, reply: str, action: str, said: str
) -> None:
    sent = _replies(monkeypatch, reply)
    runner_questions.ask_done(
        "g1", title='The agent needs someone to "sign in"', device="MacBook Pro", as_json=False
    )
    out = capsys.readouterr().out
    assert sent == [("POST", "/v1/gates/g1/resolve", {"action": action})]
    assert "Browser on MacBook Pro" in out and said in out


def test_a_device_hand_off_left_unanswered_stays_waiting(monkeypatch: Any, capsys: Any) -> None:
    sent = _replies(monkeypatch, "")
    runner_questions.ask_done(
        "g1", title="The agent needs someone at the browser", device=None, as_json=False
    )
    out = capsys.readouterr().out
    assert sent == [] and "Browser on your computer" in out
    assert "left waiting" in out and "prismoid gates resolve g1 --done" in out


def _control_stream(monkeypatch: Any, task: dict[str, Any] | Exception) -> list[str]:
    """A CONTROL gate opening on the followed stream; `task` is what `GET /v1/tasks/t1` answers.
    Returns what reached the Done prompt (its device) - or the rendered line, when none did."""
    from prismoid.runner import stream

    opened = {
        "type": "gate.opened",
        "seq": 7,
        "gate_id": "g1",
        "task_id": "t1",
        "kind": "control",
        "source": "handoff",
        "title": 'The agent needs someone to "sign in"',
        "questions": None,
    }
    lines = ["event: gate.opened", f"data: {json.dumps(opened)}"]
    monkeypatch.setattr(stream.client, "stream_sse", lambda *a, **kw: iter(lines))

    def read(method: str, path: str, **kw: Any) -> dict[str, Any]:
        assert (method, path, kw.get("raise_refused")) == ("GET", "/v1/tasks/t1", True)
        if isinstance(task, Exception):
            raise task
        return task

    monkeypatch.setattr(stream.client, "request", read)
    monkeypatch.setattr(stream.questions, "interactive", lambda: True)
    seen: list[str] = []
    monkeypatch.setattr(
        stream.questions, "ask_done", lambda gate_id, **kw: seen.append(f"done:{kw['device']}")
    )
    monkeypatch.setattr(stream.typer, "secho", lambda text, **kw: seen.append(str(text)))
    stream.follow_events("t1", follow=False, json_out=False, answer=True)
    return seen


def test_the_stream_asks_the_creator_of_a_device_task_for_done(monkeypatch: Any) -> None:
    mine = {"is_creator": True, "machine": {"driver": "device", "device_name": "MacBook Pro"}}
    assert _control_stream(monkeypatch, mine) == ["done:MacBook Pro"]


@pytest.mark.parametrize(
    "task",
    [
        {"is_creator": True, "machine": {"driver": None, "device_name": None}},
        {"is_creator": False, "machine": {"driver": "device", "device_name": "MacBook Pro"}},
    ],
    ids=["cloud", "not-mine"],
)
def test_a_cloud_or_someone_elses_control_gate_is_only_a_line(monkeypatch: Any, task: dict[str, Any]) -> None:
    (line,) = _control_stream(monkeypatch, task)
    assert "gate g1" in line and not line.startswith("done:")


def test_a_task_read_that_fails_leaves_the_gate_a_line(monkeypatch: Any) -> None:
    from prismoid.runner import client

    (line,) = _control_stream(monkeypatch, client.Refused("forbidden: no such task", 3))
    assert "gate g1" in line


def test_tasks_show_names_the_device_browser_and_what_it_waits_for() -> None:
    task: dict[str, Any] = {
        "id": "t1",
        "status": "paused",
        "goal": "find books",
        "cost": {"total_usd": "0.01", "calls": 1},
        "plan": {"steps": [], "edges": []},
        "machine": {
            "state": "parked",
            "profile": "browser",
            "driver": "device",
            "device_name": "MacBook Pro",
        },
        "waiting_for": "Waiting for MacBook Pro",
    }
    shown = tasks_cli._show(task)  # pyright: ignore[reportPrivateUsage]
    assert "  Browser on MacBook Pro" in shown.splitlines()
    assert "  Waiting for MacBook Pro" in shown.splitlines()
    away = {**task, "machine": {**task["machine"], "device_name": None}, "waiting_for": None}
    nameless = tasks_cli._show(away)  # pyright: ignore[reportPrivateUsage]
    assert "  Browser on your computer" in nameless.splitlines() and "Waiting for" not in nameless
    cloud = tasks_cli._show({**task, "machine": {**task["machine"], "driver": None}})  # pyright: ignore[reportPrivateUsage]
    assert "Browser on" not in cloud


def test_a_refused_done_says_why_and_asks_again(monkeypatch: Any, capsys: Any) -> None:
    sent: list[Any] = []
    queue = ["done", "done"]

    def fake_request(method: str, path: str, **kw: Any) -> Any:
        sent.append(kw.get("body"))
        if len(sent) == 1:
            raise runner_questions.client.Refused("forbidden: only the task's creator can say Done", 3)
        return {"action": "accept"}

    monkeypatch.setattr(runner_questions.client, "request", fake_request)
    monkeypatch.setattr(runner_questions.typer, "prompt", lambda *a, **kw: queue.pop(0))
    runner_questions.ask_done("g1", title="t", device="MacBook Pro", as_json=False)
    out = capsys.readouterr().out
    assert sent == [{"action": "accept"}, {"action": "accept"}]
    assert "only the task's creator" in out and "handed back" in out


@pytest.mark.parametrize(
    "down", [httpx.ConnectTimeout("slow"), httpx.ConnectError("refused")], ids=["timeout", "down"]
)
def test_a_device_read_that_cannot_reach_the_api_never_ends_the_stream(
    monkeypatch: Any, capsys: Any, down: Exception
) -> None:
    """`prismoid do --wait` keeps following: the read is short, and a timeout or a refused connection
    leaves the gate a line, as it always was."""
    from prismoid.runner import client, stream

    seen: dict[str, Any] = {}

    def fails(*a: Any, **kw: Any) -> Any:
        seen.update(kw)
        raise down

    monkeypatch.setattr(client, "current_token", lambda: "cs_test")
    monkeypatch.setattr(client.httpx, "request", fails)
    assert stream._device_of("t1") is None  # pyright: ignore[reportPrivateUsage]
    assert seen["timeout"] == stream.DEVICE_READ_S
    assert capsys.readouterr().err == "", "nothing said: the gate shows as its line"
