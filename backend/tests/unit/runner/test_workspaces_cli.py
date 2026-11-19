# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid workspaces` - a thin HTTP client (ADR-061). The HTTP layer is
replaced by a recorder; slug -> id and email -> actor id resolve through the list routes."""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from prismoid.runner import client, workspaces_cli
from prismoid.runner.main import app

ME = {
    "name": "Ada",
    "email": "ada@x.io",
    "workspace": {"id": "o-personal", "name": "Ada", "slug": "ada", "kind": "personal", "role": "owner"},
}
WORKSPACES = [
    ME["workspace"],
    {"id": "o-acme", "name": "Acme", "slug": "acme", "kind": "team", "role": "admin"},
]
MEMBERS = [
    {"user": {"id": "a-1", "email": "bob@x.io", "name": "Bob"}, "role": "member", "joined_at": "t"},
]


def _fake(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, Any, Any]]:
    seen: list[tuple[str, str, Any, Any]] = []

    def fake_request(method: str, path: str, *, body: Any = None, params: Any = None, **kw: Any) -> Any:
        seen.append((method, path, body, params))
        if path == "/v1/auth/me":
            return ME
        if path == "/v1/workspaces" and method == "GET":
            return WORKSPACES
        if path == "/v1/workspaces" and method == "POST":
            return {"id": "o-new", "name": body["name"], "slug": "new", "kind": "team", "role": "owner"}
        if path.endswith("/members") and method == "GET":
            return MEMBERS
        return {"ok": True}

    monkeypatch.setattr(client, "request", fake_request)
    return seen


def _run(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


def test_list_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch)
    r = _run("workspaces", "list", "--json")
    assert r.exit_code == 0
    assert [o["slug"] for o in json.loads(r.stdout)] == ["ada", "acme"]


def test_switch_resolves_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch)
    r = _run("workspaces", "switch", "acme")
    assert r.exit_code == 0
    assert ("PUT", "/v1/auth/session/workspace", {"workspace_id": "o-acme"}, None) in seen


def test_switch_unknown_slug_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch)
    r = _run("workspaces", "switch", "nope")
    assert r.exit_code == client.EXIT_ERROR
    assert not any(c[0] == "PUT" for c in seen)


def test_create(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch)
    r = _run("workspaces", "create", "Acme Two", "--json")
    assert r.exit_code == 0
    assert ("POST", "/v1/workspaces", {"name": "Acme Two"}, None) in seen


def test_members_use_active_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch)
    r = _run("workspaces", "members", "--json")
    assert r.exit_code == 0
    assert ("GET", "/v1/workspaces/o-personal/members", None, None) in seen


def test_invite(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch)
    r = _run("workspaces", "invite", "c@x.io", "--role", "guest")
    assert r.exit_code == 0
    assert ("POST", "/v1/workspaces/o-personal/invites", {"email": "c@x.io", "role": "guest"}, None) in seen


def test_invite_bad_role_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch)
    assert _run("workspaces", "invite", "c@x.io", "--role", "owner").exit_code != 0


def test_role_and_remove_resolve_email(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch)
    assert _run("workspaces", "role", "BOB@x.io", "admin").exit_code == 0
    assert ("PATCH", "/v1/workspaces/o-personal/members/a-1", {"role": "admin"}, None) in seen
    assert _run("workspaces", "remove", "bob@x.io").exit_code == 0
    assert ("DELETE", "/v1/workspaces/o-personal/members/a-1", None, None) in seen


def test_unknown_member_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch)
    assert _run("workspaces", "remove", "ghost@x.io").exit_code == client.EXIT_ERROR


def test_a_workspace_command_with_no_workspace_left_says_how_back_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/v1/auth/me` answers `workspace: null` once every membership is gone: the active-workspace
    commands refuse in words, as the door's 403 would, and name the way back in."""
    _fake(monkeypatch)
    monkeypatch.setitem(ME, "workspace", None)
    r = _run("workspaces", "members")
    assert r.exit_code == client.EXIT_DENIED
    assert "no active workspace" in r.stderr and "prismoid workspaces create" in r.stderr


def test_whoami_prints_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch)
    r = _run("whoami")
    assert "ada" in r.stdout and "personal" in r.stdout and "owner" in r.stdout


def test_keys_create_prints_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(method: str, path: str, **kw: Any) -> Any:
        return {"key": "ck_x", "workspace_id": "o-acme"}

    monkeypatch.setattr(client, "request", fake)
    assert "o-acme" in _run("keys", "create", "k").stdout


def test_module_exports() -> None:
    assert workspaces_cli.workspaces is not None
