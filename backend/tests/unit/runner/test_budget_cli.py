# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid tasks budget` (B3): set the ceiling, and with --resume accept the gate the raise names."""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from prismoid.api.serializers.tasks import TaskBudgetIn
from prismoid.runner import client
from prismoid.runner.main import app


def _fake(monkeypatch: pytest.MonkeyPatch, gate_id: str | None) -> list[tuple[str, str, Any]]:
    sent: list[tuple[str, str, Any]] = []

    def fake_request(method: str, path: str, **kw: Any) -> Any:
        sent.append((method, path, kw.get("body")))
        if method == "PUT":
            return {"usd": "2.00", "previous_usd": "0.01", "gate_id": gate_id}
        return {"action": "accept", "resolved_by": "Bob"}

    monkeypatch.setattr(client, "request", fake_request)
    return sent


def test_budget_sends_a_body_the_api_accepts(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _fake(monkeypatch, gate_id="g1")
    result = CliRunner().invoke(app, ["tasks", "budget", "t1", "--usd", "2"])
    assert result.exit_code == 0, result.output
    [(method, path, body)] = sent
    assert (method, path) == ("PUT", "/v1/tasks/t1/budget")
    TaskBudgetIn.model_validate(body)


def test_resume_accepts_the_gate_the_raise_names(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _fake(monkeypatch, gate_id="g1")
    result = CliRunner().invoke(app, ["tasks", "budget", "t1", "--usd", "2", "--resume", "--json"])
    assert result.exit_code == 0, result.output
    assert sent[1] == ("POST", "/v1/gates/g1/resolve", {"action": "accept"})


def test_resume_with_no_open_gate_fails_honestly(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch, gate_id=None)
    result = CliRunner().invoke(app, ["tasks", "budget", "t1", "--usd", "2", "--resume"])
    assert result.exit_code == 1 and "no open budget gate" in result.output


def _refusing(monkeypatch: pytest.MonkeyPatch, refusal: str) -> None:
    """The raise lands; the resolve is refused the way the real client refuses - a `Refused` for a
    caller that asked to handle it, else the error envelope and the exit."""

    def fake_request(method: str, path: str, **kw: Any) -> Any:
        if method == "PUT":
            return {"usd": "2.00", "previous_usd": "0.01", "gate_id": "g1"}
        if kw.get("raise_refused"):
            raise client.Refused(refusal, client.EXIT_ERROR)
        client.fail(refusal, as_json=kw.get("as_json", False))

    monkeypatch.setattr(client, "request", fake_request)


def test_resume_json_with_no_open_gate_still_reports_the_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fix wave B6: the raise LANDED - a script reading --json must see it, and the exit says the
    resume did not happen."""
    _fake(monkeypatch, gate_id=None)
    result = CliRunner().invoke(app, ["tasks", "budget", "t1", "--usd", "2", "--resume", "--json"])
    assert result.exit_code == 1
    out = json.loads(result.stdout)
    assert out["usd"] == "2.00" and out["resumed"] is False and "no open budget gate" in out["error"]
    assert "no open budget gate" in result.stderr


def test_resume_json_with_a_refused_resolve_still_reports_the_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    refusal = (
        "conflict: The Project's monthly budget is still spent; its owner can raise it in Project settings"
    )
    _refusing(monkeypatch, refusal)
    result = CliRunner().invoke(app, ["tasks", "budget", "t1", "--usd", "2", "--resume", "--json"])
    assert result.exit_code == 1
    out = json.loads(result.stdout)
    assert out["usd"] == "2.00" and out["resumed"] is False and refusal in out["error"]
    assert "Project settings" in result.stderr


def test_resume_with_a_refused_resolve_says_the_raise_landed(monkeypatch: pytest.MonkeyPatch) -> None:
    _refusing(monkeypatch, "conflict: The Project's monthly budget is still spent")
    result = CliRunner().invoke(app, ["tasks", "budget", "t1", "--usd", "2", "--resume"])
    assert result.exit_code == 1
    assert "budget $2.00 (was $0.01)" in result.stdout and "still spent" in result.stderr
