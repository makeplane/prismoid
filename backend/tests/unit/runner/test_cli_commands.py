# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The `prismoid` commands through the real Typer app: what each one sends, what `--json` prints and
which exit code it ends with. The API is a fake `client.request` that answers by route, so every
command runs end to end in-process with no server; the HTTP-to-exit-code mapping is tested against
a fake `httpx.request` underneath the real client."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from prismoid.runner import client
from prismoid.runner.main import app

Sent = list[tuple[str, str, Any]]
Routes = dict[tuple[str, str], Any]


def _api(monkeypatch: pytest.MonkeyPatch, routes: Routes) -> Sent:
    """Answer `client.request` from `routes` by (method, path); record what was sent."""
    sent: Sent = []

    def fake_request(method: str, path: str, **kw: Any) -> Any:
        sent.append((method, path, kw.get("body")))
        answer = routes[(method, path)]
        return answer(kw) if callable(answer) else answer

    monkeypatch.setattr(client, "request", fake_request)
    return sent


def _prismoid(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


def _json(result: Any) -> Any:
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


# ── session auth and keys ─────────────────────────────────────────────────────────────────────


def test_login_stores_the_token_and_never_prints_it(monkeypatch: pytest.MonkeyPatch) -> None:
    saved: list[tuple[str, str]] = []
    monkeypatch.setattr(client, "save_token", lambda token, url: saved.append((token, url)))
    monkeypatch.setattr(client, "api_url", lambda: "http://api.test")
    answer = {"token": "cs_secret", "user": {"email": "ada@x.test"}, "expires_at": "T"}
    sent = _api(monkeypatch, {("POST", "/v1/auth/login"): answer})
    r = _prismoid("login", "--email", "ada@x.test", "--password", "pw", "--json")
    assert _json(r) == {"email": "ada@x.test", "expires_at": "T"} and "cs_secret" not in r.output
    assert saved == [("cs_secret", "http://api.test")]
    assert sent == [("POST", "/v1/auth/login", {"email": "ada@x.test", "password": "pw"})]


def test_signup_names_the_account_from_the_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "save_token", lambda token, url: None)
    answer = {"token": "cs_x", "user": {"email": "ada@x.test"}, "expires_at": "T"}
    sent = _api(monkeypatch, {("POST", "/v1/auth/signup"): answer})
    r = _prismoid("login", "--email", "ada@x.test", "--password", "pw", "--signup")
    assert r.exit_code == 0 and r.stdout.strip() == "logged in as ada@x.test"
    assert sent[0][2] == {"email": "ada@x.test", "password": "pw", "name": "ada"}


def test_logout_and_reset_forget_the_stored_token(monkeypatch: pytest.MonkeyPatch) -> None:
    cleared: list[bool] = []
    monkeypatch.setattr(client, "clear_token", lambda: cleared.append(True))
    sent = _api(monkeypatch, {("POST", "/v1/auth/logout"): None, ("POST", "/v1/auth/reset-password"): None})
    assert _json(_prismoid("logout", "--json")) == {"ok": True}
    r = _prismoid("reset-password", "--code", " cr_abc ", "--password", "a-long-password", "--json")
    assert _json(r) == {"ok": True} and len(cleared) == 2
    assert sent[-1] == (
        "POST",
        "/v1/auth/reset-password",
        {"code": "cr_abc", "new_password": "a-long-password"},
    )


def test_forgot_password_says_where_the_code_goes(monkeypatch: pytest.MonkeyPatch) -> None:
    _api(monkeypatch, {("POST", "/v1/auth/forgot-password"): None})
    r = _prismoid("forgot-password", "--email", "ada@x.test")
    assert r.exit_code == 0 and "prismoid reset-password" in r.stdout


def test_whoami_shows_the_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    me = {
        "name": "Ada",
        "email": "ada@x.test",
        "workspace": {"name": "Lab", "slug": "lab", "kind": "team", "role": "owner"},
    }
    _api(monkeypatch, {("GET", "/v1/auth/me"): me})
    assert _json(_prismoid("whoami", "--json")) == me
    assert "workspace: Lab (lab, team) - owner" in _prismoid("whoami").stdout


def test_keys_create_list_and_revoke(monkeypatch: pytest.MonkeyPatch) -> None:
    key = {"id": "k1", "key": "ck_plain", "workspace_id": "w1"}
    rows = [{"id": "k1", "name": "ci", "revoked_at": None}, {"id": "k2", "name": "old", "revoked_at": "T"}]
    sent = _api(
        monkeypatch,
        {
            ("POST", "/v1/auth/api-keys"): key,
            ("GET", "/v1/auth/api-keys"): rows,
            ("DELETE", "/v1/auth/api-keys/k1"): None,
        },
    )
    assert _json(_prismoid("keys", "create", "ci", "--json")) == key
    assert sent[-1][2] == {"name": "ci"}
    listed = _prismoid("keys", "list").stdout
    assert "k1  ci" in listed and "active" in listed and "revoked" in listed
    assert _json(_prismoid("keys", "revoke", "k1", "--json")) == {"ok": True}


# ── tasks ─────────────────────────────────────────────────────────────────────────────────────

TASK: dict[str, Any] = {
    "id": "t1",
    "status": "running",
    "goal": "write the memo",
    "cost": {"total_usd": "0.42", "calls": 3},
    "size_marker": "standard · ~5 pages",
    "plan": {
        "steps": [
            {"id": "s1", "state": "done", "agent": "researcher", "title": "Read"},
            {"id": "s2", "state": "running", "agent": "writer", "title": "Write", "attempt": 2},
        ],
        "edges": [{"from_step": "s1", "to_step": "s2"}],
        "criteria": {"must": ["cite every number"]},
    },
    "machine": {"state": "idle", "profile": "code"},
}


def test_tasks_list_filters_by_status(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"id": "t1", "status": "running", "goal": "write the memo"}]
    seen: list[Any] = []

    def page(kw: dict[str, Any]) -> Any:
        seen.append(kw.get("params"))
        return {"items": rows}

    _api(monkeypatch, {("GET", "/v1/tasks"): page})
    assert _json(_prismoid("tasks", "list", "--status", "running", "--json")) == rows
    assert seen == [{"status": "running"}]
    assert _prismoid("tasks", "list").stdout.startswith("t1  running")


def test_tasks_show_draws_the_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    _api(monkeypatch, {("GET", "/v1/tasks/t1"): TASK})
    assert _json(_prismoid("tasks", "show", "t1", "--json")) == TASK
    out = _prismoid("tasks", "show", "t1").stdout
    assert "size:    standard · ~5 pages" in out
    assert "✓ #1 [researcher] Read" in out and "▸ #2 [writer] Write  (after #1)  attempt 2" in out
    assert "· cite every number" in out and "machine: Idle · code" in out


def test_tasks_show_refuses_an_empty_id(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _api(monkeypatch, {})
    r = _prismoid("tasks", "show", " ", "--json")
    assert r.exit_code == client.EXIT_ERROR and sent == []


@pytest.mark.parametrize(
    ("args", "route", "said"),
    [
        (["pause"], ("POST", "/v1/tasks/t1/pause"), "pausing at the next safe point"),
        (["resume"], ("POST", "/v1/tasks/t1/resume"), "resumed"),
        (["cancel"], ("POST", "/v1/tasks/t1/cancel"), "cancelled"),
    ],
)
def test_the_one_call_task_verbs(
    monkeypatch: pytest.MonkeyPatch, args: list[str], route: tuple[str, str], said: str
) -> None:
    _api(monkeypatch, {route: {"ok": True}})
    assert _json(_prismoid("tasks", args[0], "t1", "--json")) == {"ok": True}
    assert _prismoid("tasks", args[0], "t1").stdout.strip() == said


def test_tasks_replan_and_move(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _api(
        monkeypatch,
        {("POST", "/v1/tasks/t1/replan"): {"steps": 2}, ("POST", "/v1/tasks/t1/move"): {"ok": True}},
    )
    assert "replanned: 2 new step(s)" in _prismoid("tasks", "replan", "t1", "too long").stdout
    assert sent[-1][2] == {"directive": "too long"}
    assert _prismoid("tasks", "move", "t1", "--personal").stdout.strip() == "moved to personal"
    assert sent[-1][2] == {"project_id": None}
    assert _json(_prismoid("tasks", "move", "t1", "--project", "p1", "--json")) == {"ok": True}
    assert sent[-1][2] == {"project_id": "p1"}
    assert _prismoid("tasks", "move", "t1").exit_code == client.EXIT_ERROR


@pytest.mark.parametrize(
    ("by", "line"), [([], "tokens_in"), (["--by", "family"], "anthropic"), (["--by", "person"], "Ada")]
)
def test_tasks_usage_breaks_the_cost_down(monkeypatch: pytest.MonkeyPatch, by: list[str], line: str) -> None:
    usage = {
        "units": {"tokens_in": 1200},
        "by_family": {"anthropic": "0.40"},
        "by_person": [{"name": "Ada", "usd": "0.40"}],
        "total_usd": "0.40",
        "calls": 2,
    }
    _api(monkeypatch, {("GET", "/v1/tasks/t1/usage"): usage})
    assert _json(_prismoid("tasks", "usage", "t1", "--json")) == usage
    out = _prismoid("tasks", "usage", "t1", *by).stdout
    assert line in out and "TOTAL" in out and "(2 calls)" in out


@pytest.mark.parametrize(("verb", "path"), [("follow-up", "follow_up"), ("retry", "retry")])
def test_a_next_turn_without_wait_says_how_to_follow_it(
    monkeypatch: pytest.MonkeyPatch, verb: str, path: str
) -> None:
    accepted = {"task_id": "t1", "turn": 3, "last_seq": 41}
    sent = _api(monkeypatch, {("POST", f"/v1/tasks/t1/{path}"): accepted})
    args = ["tasks", verb, "t1", *(["now in French"] if verb == "follow-up" else [])]
    assert _json(_prismoid(*args, "--json")) == accepted
    assert "prismoid tasks events t1 -f --since 40" in _prismoid(*args).stdout
    if verb == "follow-up":
        assert sent[-1][2] == {"goal": "now in French"}


def test_do_attaches_an_input_file_by_its_basename(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    brief = tmp_path / "notes" / "brief.md"
    brief.parent.mkdir()
    brief.write_bytes(b"# Q3\n")
    sent = _api(
        monkeypatch, {("GET", "/healthz"): {"device": False}, ("POST", "/v1/tasks"): {"task_id": "t9"}}
    )
    r = _prismoid(
        "do", "memo", "--no-wait", "--json", "-i", str(brief), "--project", "p1", "--budget-usd", "2"
    )
    assert _json(r) == {"task_id": "t9"}
    body = sent[-1][2]
    assert body["inputs"] == [{"filename": "brief.md", "content": "IyBRMwo=", "media_type": "text/markdown"}]
    assert (body["project_id"], body["budget"]) == ("p1", {"usd": 2.0})


def test_do_refuses_a_missing_input_before_the_wire(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sent = _api(monkeypatch, {("GET", "/healthz"): {"device": False}})
    r = _prismoid("do", "memo", "--no-wait", "--json", "-i", str(tmp_path / "nope.md"))
    assert r.exit_code == client.EXIT_ERROR and json.loads(r.stderr) == {
        "error": f"no such file: {tmp_path / 'nope.md'}"
    }
    assert all(path != "/v1/tasks" for _, path, _ in sent)


# ── gates, projects, artifacts, the roster ────────────────────────────────────────────────────


def test_gates_list_counts_the_questions(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"id": "g1", "title": "Which currency?", "questions": {"questions": [{}, {}]}}]
    _api(monkeypatch, {("GET", "/v1/gates"): rows})
    assert _json(_prismoid("gates", "list", "--json")) == rows
    assert _prismoid("gates", "list").stdout.strip() == "g1  Which currency?  (2 questions)"


PROJECT = {"id": "p1", "name": "Lab", "role": "owner", "budget_usd": "50"}


def test_projects_create_list_show_and_update(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _api(
        monkeypatch,
        {
            ("POST", "/v1/projects"): PROJECT,
            ("GET", "/v1/projects"): {"items": [PROJECT]},
            ("GET", "/v1/projects/p1"): PROJECT,
            ("PATCH", "/v1/projects/p1"): PROJECT,
        },
    )
    assert _json(_prismoid("projects", "create", "Lab", "--budget-usd", "50", "--json")) == PROJECT
    assert sent[-1][2] == {"name": "Lab", "budget_usd": 50.0}
    assert "p1  Lab" in _prismoid("projects", "list").stdout
    assert "budget: $50" in _prismoid("projects", "show", "p1").stdout
    assert _prismoid("projects", "update", "p1", "--name", "Lab 2", "--clear-budget").exit_code == 0
    assert sent[-1][2] == {"name": "Lab 2", "clear_budget": True}


def test_projects_members_share_and_unshare(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _api(
        monkeypatch,
        {
            ("POST", "/v1/projects/p1/members"): {"role": "editor"},
            ("GET", "/v1/projects/p1/members"): [{"actor_id": "a2", "role": "editor"}],
            ("DELETE", "/v1/projects/p1/members/a2"): None,
            ("GET", "/v1/projects/p1/usage"): {
                "budget_usd": None,
                "remaining_usd": None,
                "month_to_date_usd": "3",
            },
            ("GET", "/v1/projects/p1/artifacts"): [],
        },
    )
    assert "as editor" in _prismoid("projects", "share", "p1", "bo@x.test", "--role", "editor").stdout
    assert sent[-1][2] == {"email": "bo@x.test", "role": "editor"}
    assert _prismoid("projects", "members", "p1").stdout.strip() == "a2  editor"
    assert _json(_prismoid("projects", "unshare", "p1", "a2", "--json")) == {"ok": True}
    assert "MTD: $3 / unlimited" in _prismoid("projects", "usage", "p1").stdout
    assert _prismoid("projects", "artifacts", "p1").stdout.strip() == "no artifacts yet"


ARTIFACT = {
    "id": "a1",
    "title": "Memo",
    "type": "document",
    "sha256": "ab" * 32,
    "bytes": 2048,
    "verdict": "pass",
    "provenance": {"agent": "writer", "model": "m", "sources": ["brief.md"]},
}
REPORT = {
    "verdict": "fail",
    "suite": "document",
    "producer_model": "m1",
    "producer_family": "anthropic",
    "verifier_model": "m2",
    "verifier_family": "google",
    "signature_valid": True,
    "algorithm": "ed25519",
    "checks": [{"name": "numbers:grounded", "status": "fail", "detail": "1 ungrounded", "actual": "9.9"}],
    "judge_rationale": "one number has no source",
}


def test_artifacts_show_and_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    _api(
        monkeypatch,
        {("GET", "/v1/artifacts/a1"): ARTIFACT, ("GET", "/v1/artifacts/a1/verification"): REPORT},
    )
    assert _json(_prismoid("artifacts", "show", "a1", "--json")) == ARTIFACT
    assert "verdict: pass" in _prismoid("artifacts", "show", "a1").stdout
    out = _prismoid("artifacts", "verify", "a1").stdout
    assert "verdict:  FAIL" in out and "→ 9.9" in out and "other family" in out
    assert _prismoid("artifacts", "verify", "a1", "--assert-verified").exit_code == 1


def test_artifacts_revisions_restore_and_label(monkeypatch: pytest.MonkeyPatch) -> None:
    revisions = {
        "items": [
            {
                "revision": 2,
                "id": "a2",
                "provenance": {"agent": "writer"},
                "sha256": "cd" * 32,
                "label": "final",
            },
            {"revision": 1, "id": "a1", "provenance": {}, "sha256": "ab" * 32},
        ]
    }
    sent = _api(
        monkeypatch,
        {
            ("GET", "/v1/artifacts/a1/revisions"): revisions,
            ("POST", "/v1/artifacts/a1/restore"): {"revision": 3, "id": "a3"},
            ("POST", "/v1/artifacts/a1/label"): lambda kw: {"revision": 1, "label": kw["body"]["label"]},
        },
    )
    out = _prismoid("artifacts", "revisions", "a1").stdout.splitlines()
    assert out[0].startswith("v2  a2  writer") and out[0].endswith("«final» · latest")
    assert "restored as v3 (a3)" in _prismoid("artifacts", "restore", "a1", "--base", "cd").stdout
    assert sent[-1][2] == {"base_sha256": "cd"}
    assert "v1 labelled «draft»" in _prismoid("artifacts", "label", "a1", "draft").stdout
    assert "v1 label cleared" in _prismoid("artifacts", "label", "a1", "--clear").stdout
    assert _prismoid("artifacts", "label", "a1").exit_code == client.EXIT_ERROR
    assert _prismoid("artifacts", "label", "a1", "x", "--clear").exit_code == client.EXIT_ERROR


def test_agents_lists_the_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"name": "writer", "capability": "fast-executor", "description": "Writes."}]
    _api(monkeypatch, {("GET", "/v1/agents"): rows})
    assert _json(_prismoid("agents", "--json")) == rows
    assert _prismoid("agents").stdout.startswith("writer")


def test_inbox_lists_and_marks_read(monkeypatch: pytest.MonkeyPatch) -> None:
    item = {
        "id": "i1",
        "read": False,
        "kind": "gate_opened",
        "title": "A question",
        "body": "Which?\nmore",
        "task_id": "t1",
    }
    page = {"items": [item], "unread": 1}
    sent = _api(
        monkeypatch,
        {
            ("GET", "/v1/inbox?unread=true"): page,
            ("GET", "/v1/inbox?unread=false"): {"items": [], "unread": 0},
            ("POST", "/v1/inbox/i1/read"): item,
            ("POST", "/v1/inbox/read-all"): {"ok": True},
            ("POST", "/v1/inbox/tasks/t1/read"): {"marked": 2, "unread": 3},
        },
    )
    assert _json(_prismoid("inbox", "--unread", "--json")) == page
    out = _prismoid("inbox", "--unread").stdout
    assert "• ? A question" in out and "→ prismoid tasks show t1" in out and "1 unread" in out
    assert _prismoid("inbox").stdout.strip() == "inbox empty"
    assert _prismoid("inbox", "read", "i1").stdout.strip() == "read: A question"
    assert _json(_prismoid("inbox", "read", "--all", "--json")) == {"ok": True}
    assert sent[-1][1] == "/v1/inbox/read-all"
    assert _prismoid("inbox", "read", "--task", "t1").stdout.strip() == "read 2 · 3 unread"
    assert _json(_prismoid("inbox", "read", "--task", "t1", "--json")) == {"marked": 2, "unread": 3}
    assert sent[-1][1] == "/v1/inbox/tasks/t1/read"
    assert _prismoid("inbox", "read").exit_code == client.EXIT_ERROR


def test_capture_add_list_and_promote(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _api(
        monkeypatch,
        {
            ("POST", "/v1/capture"): {"id": "c1"},
            ("GET", "/v1/capture"): [{"id": "c1", "text": "an idea"}],
            ("POST", "/v1/capture/c1/promote"): {"task_id": "t2"},
        },
    )
    assert _prismoid(
        "capture", "add", "an idea", "--url", "https://x.test", "--project", "p1"
    ).stdout.strip() == ("c1  captured")
    assert sent[-1][2] == {"text": "an idea", "url": "https://x.test", "project_id": "p1"}
    assert _prismoid("capture", "list").stdout.strip() == "c1  an idea"
    assert _json(_prismoid("capture", "promote", "c1", "--json")) == {"task_id": "t2"}


def test_run_api_serves_the_app_with_the_options(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    served: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app_path, **kw: served.append({"app": app_path, **kw}))
    assert _prismoid("run", "api", "--host", "127.0.0.9", "--port", "9999").exit_code == 0
    (call,) = served
    assert (call["app"], call["host"], call["port"], call["access_log"]) == (
        "prismoid.api.app:app",
        "127.0.0.9",
        9999,
        False,
    )


# ── what the API answers, as an exit code ────────────────────────────────────────────────────


def _http(monkeypatch: pytest.MonkeyPatch, respond: Callable[..., httpx.Response]) -> None:
    monkeypatch.setattr(client, "current_token", lambda: "cs_test")
    monkeypatch.setattr(httpx, "request", respond)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, client.EXIT_DENIED),
        (403, client.EXIT_DENIED),
        (404, client.EXIT_ERROR),
        (500, client.EXIT_ERROR),
    ],
)
def test_an_error_response_maps_to_its_exit_code(
    monkeypatch: pytest.MonkeyPatch, status: int, code: int
) -> None:
    envelope = {"error": {"code": "nope", "message": "not for you"}}
    _http(monkeypatch, lambda *a, **kw: httpx.Response(status, json=envelope))
    r = _prismoid("tasks", "show", "t1", "--json")
    assert r.exit_code == code and json.loads(r.stderr) == {"error": "nope: not for you"}


def test_a_timeout_exits_3_and_no_credentials_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*a: Any, **kw: Any) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    _http(monkeypatch, slow)
    assert _prismoid("tasks", "show", "t1").exit_code == client.EXIT_TIMEOUT
    monkeypatch.setattr(client, "current_token", lambda: None)
    r = _prismoid("tasks", "list")
    assert r.exit_code == client.EXIT_DENIED and "prismoid login" in r.output


def test_a_refused_connection_names_the_address(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(*a: Any, **kw: Any) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _http(monkeypatch, down)
    monkeypatch.setattr(client, "api_url", lambda: "http://api.test")
    r = _prismoid("agents")
    assert r.exit_code == client.EXIT_ERROR and "cannot reach http://api.test" in r.output
