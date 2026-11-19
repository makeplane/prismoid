# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid connectors authorize` - a thin HTTP client (ADR-020). The HTTP layer is replaced by
a recorder. An OAuth sign-in finishes in the web app; the command only points there, so it refuses
a row with no sign-in to finish: a connector that does not use OAuth, and one already live."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from prismoid.runner import client
from prismoid.runner.main import app

ROWS = [
    {"id": "c-1", "slug": "notion", "name": "Notion", "auth_kind": "oauth", "status": "pending_auth"},
    {"id": "c-2", "slug": "linear", "name": "Linear", "auth_kind": "oauth", "status": "live"},
    {"id": "c-3", "slug": "internal", "name": "Internal", "auth_kind": "bearer", "status": "live"},
    {"id": "c-4", "slug": "open", "name": "Open", "auth_kind": "none", "status": "error"},
]


def _fake(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, **_: Any) -> Any:
        seen.append((method, path))
        if path == "/v1/connectors":
            return {"items": ROWS}
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(client, "request", fake_request)
    return seen


def _run(*args: str) -> Any:
    return CliRunner().invoke(app, ["connectors", "authorize", *args])


def test_an_oauth_row_waiting_for_its_sign_in_points_at_the_web(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch)
    r = _run("notion", "--no-wait")
    assert r.exit_code == 0, r.output
    assert "Connectors page" in r.output


def test_a_live_oauth_row_is_refused_and_re_consent_points_at_the_web(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake(monkeypatch)
    r = _run("linear")
    assert r.exit_code == 1
    assert "already connected" in r.output and "Connectors page" in r.output
    assert "tool(s)" not in r.output  # never the poll's success line


@pytest.mark.parametrize("slug", ["internal", "open"])
def test_a_connector_without_oauth_has_nothing_to_authorize(
    monkeypatch: pytest.MonkeyPatch, slug: str
) -> None:
    seen = _fake(monkeypatch)
    r = _run(slug)
    assert r.exit_code == 1
    assert "OAuth" in r.output
    assert seen == [("GET", "/v1/connectors")]  # no poll: it would wait for a sign-in that never comes
