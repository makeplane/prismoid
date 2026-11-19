# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid tasks steer` (ADR-064): `--epoch` stamps the steer, a stale one exits 4 naming what
was missed, and `--retract` withdraws your own."""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from prismoid.runner import client
from prismoid.runner.main import app

STALE = {
    "code": "stale_steer",
    "message": "Bob steered while you were typing: 'use euros'",
    "missed": [{"kind": "steer", "author": "Bob", "text": "use euros", "epoch": 2, "state": "accepted"}],
}


def _stale(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    def fake_request(method: str, path: str, **kw: Any) -> Any:
        sent.append({"method": method, "path": path, **kw})
        raise client.Conflicted(STALE)

    monkeypatch.setattr(client, "request", fake_request)
    return sent


def test_a_stale_steer_exits_4_and_names_what_was_missed(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _stale(monkeypatch)
    r = CliRunner().invoke(app, ["tasks", "steer", "t", "x", "--epoch", "1"])
    assert r.exit_code == client.EXIT_CONFLICT == 4
    assert sent[0]["body"] == {"directive": "x", "issued_epoch": 1} and sent[0]["raise_conflict"] is True
    assert "Bob steered while you were typing" in r.stderr and "missed: Bob steer use euros" in r.stderr


def test_a_stale_steer_in_json_keeps_the_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    _stale(monkeypatch)
    r = CliRunner().invoke(app, ["tasks", "steer", "t", "x", "--epoch", "1", "--json"])
    assert r.exit_code == 4 and json.loads(r.stderr)["error"]["missed"][0]["author"] == "Bob"


def test_a_steer_prints_its_epoch_and_command(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, str, Any]] = []

    def fake_request(method: str, path: str, **kw: Any) -> Any:
        sent.append((method, path, kw.get("body")))
        return {"steered": "use euros", "command_id": "c-1", "epoch": 2, "state": "accepted"}

    monkeypatch.setattr(client, "request", fake_request)
    r = CliRunner().invoke(app, ["tasks", "steer", "t", "use euros"])
    assert r.exit_code == 0 and sent == [("POST", "/v1/tasks/t/steer", {"directive": "use euros"})]
    assert r.stdout.strip() == "steered (epoch 2, accepted): use euros  [c-1]"


def test_retract_posts_to_the_retract_path(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, **kw: Any) -> Any:
        sent.append((method, path))
        return {"steered": "use euros", "command_id": "c-1", "epoch": 2, "state": "retracted"}

    monkeypatch.setattr(client, "request", fake_request)
    r = CliRunner().invoke(app, ["tasks", "steer", "t", "--retract", "c-1"])
    assert r.exit_code == 0 and sent == [("POST", "/v1/tasks/t/steers/c-1/retract")]
    assert "retracted c-1" in r.stdout


def test_a_blank_steer_is_refused_before_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, path: str, **kw: Any) -> Any:
        raise AssertionError("nothing should be sent")

    monkeypatch.setattr(client, "request", fake_request)
    r = CliRunner().invoke(app, ["tasks", "steer", "t", "   "])
    assert r.exit_code == client.EXIT_ERROR and "a steer needs words" in r.stderr
