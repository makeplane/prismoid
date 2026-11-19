# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid projects invite-link | invite-links | revoke-link | join` (ADR-062) - a thin HTTP
client. The HTTP layer is replaced by a recorder; the round trip through the real API is
`tests/integration/workspaces/test_projects_cli.py`."""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from prismoid.runner import client
from prismoid.runner.main import app

CODE = "si_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
MINTED = {
    "id": "inv-1",
    "code": CODE,
    "role": "editor",
    "uses": 0,
    "expires_at": "2026-10-07T00:00:00Z",
    "created_at": "2026-09-30T00:00:00Z",
    "state": "live",
}
LISTED = [{k: v for k, v in MINTED.items() if k != "code"}]
ACCEPTED = {"project_id": "sp-1", "workspace_id": "o-1", "role": "editor"}

Call = tuple[str, str, Any]


def _fake(monkeypatch: pytest.MonkeyPatch) -> list[Call]:
    seen: list[Call] = []

    def fake_request(method: str, path: str, *, body: Any = None, **kw: Any) -> Any:
        seen.append((method, path, body))
        if method == "POST" and path.endswith("/invites"):
            return MINTED
        if method == "GET" and path.endswith("/invites"):
            return LISTED
        if method == "POST" and path == "/v1/projects/invites/accept":
            return ACCEPTED
        return None  # a 204

    monkeypatch.setattr(client, "request", fake_request)
    return seen


def _run(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


def test_invite_link_sends_only_the_role_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch)
    r = _run("projects", "invite-link", "sp-1")
    assert r.exit_code == 0, r.output
    assert seen == [("POST", "/v1/projects/sp-1/invites", {"role": "editor"})]  # the server owns the defaults


def test_invite_link_carries_role_and_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch)
    r = _run("projects", "invite-link", "sp-1", "--role", "viewer", "--expires", "3")
    assert r.exit_code == 0, r.output
    assert seen == [("POST", "/v1/projects/sp-1/invites", {"role": "viewer", "expires_in_days": 3})]


def test_invite_link_offers_no_use_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every link is single-use: `--uses` is gone, refused by the
    parser before any request."""
    seen = _fake(monkeypatch)
    r = _run("projects", "invite-link", "sp-1", "--uses", "2")
    assert r.exit_code == 2, r.output  # click's usage error
    assert seen == []


def test_invite_link_human_output_shows_the_code_once_and_where_to_open_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake(monkeypatch)
    r = _run("projects", "invite-link", "sp-1")
    assert CODE in r.stdout
    assert f"open /join/{CODE} in the app" in r.stdout  # the CLI knows no web URL to print
    assert "editor" in r.stdout and "lets one person in" in r.stdout


def test_invite_link_json_is_the_created_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch)
    r = _run("projects", "invite-link", "sp-1", "--json")
    assert json.loads(r.stdout) == MINTED


def test_invite_link_rejects_an_owner_role_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch)
    r = _run("projects", "invite-link", "sp-1", "--role", "owner")
    assert r.exit_code == 2  # a usage error: the value is outside the option's choices
    assert seen == []


def test_invite_links_lists_without_the_code(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch)
    r = _run("projects", "invite-links", "sp-1")
    assert r.exit_code == 0, r.output
    assert seen == [("GET", "/v1/projects/sp-1/invites", None)]
    assert "inv-1" in r.stdout and "editor" in r.stdout and "live" in r.stdout
    assert "/" not in r.stdout.split("expires")[0]  # no use count: a link is single-use
    assert json.loads(_run("projects", "invite-links", "sp-1", "--json").stdout) == LISTED


def test_invite_links_says_so_when_there_are_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "request", lambda *a, **kw: [])  # pyright: ignore[reportUnknownLambdaType]
    assert "no invite links" in _run("projects", "invite-links", "sp-1").stdout


def test_revoke_link_deletes_the_invite(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch)
    r = _run("projects", "revoke-link", "sp-1", "inv-1", "--json")
    assert r.exit_code == 0, r.output
    assert seen == [("DELETE", "/v1/projects/sp-1/invites/inv-1", None)]
    assert json.loads(r.stdout) == {"ok": True}


def test_join_posts_the_code(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch)
    r = _run("projects", "join", CODE)
    assert r.exit_code == 0, r.output
    assert seen == [("POST", "/v1/projects/invites/accept", {"code": CODE})]
    assert "sp-1" in r.stdout and "editor" in r.stdout
    assert json.loads(_run("projects", "join", CODE, "--json").stdout) == ACCEPTED


@pytest.mark.parametrize(
    "pasted",
    [f"/join/{CODE}", f"https://app.example.com/join/{CODE}", f"https://app.example.com/join/{CODE}?x=1#top"],
)
def test_join_takes_a_pasted_link_as_well_as_the_bare_code(
    monkeypatch: pytest.MonkeyPatch, pasted: str
) -> None:
    seen = _fake(monkeypatch)
    assert _run("projects", "join", pasted).exit_code == 0
    assert seen == [("POST", "/v1/projects/invites/accept", {"code": CODE})]
