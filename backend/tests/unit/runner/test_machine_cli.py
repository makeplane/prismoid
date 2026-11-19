# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid tasks machine` - the Machine from the terminal."""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from prismoid.runner import client
from prismoid.runner.main import app

MACHINE = {
    "state": "asleep",
    "profile": "browser",
    "state_since": "2026-09-30T14:02:00Z",
    "last_active_at": "2026-09-30T13:52:00Z",
    "checkpoint_at": "2026-09-30T14:02:00Z",
    "checkpoint_bytes": 356515840,
    "browser_saved": True,
    "warm_seconds": 2280,
    "cost_usd": "2.85",
}


def _fake(monkeypatch: pytest.MonkeyPatch, machine: dict[str, Any] | None = MACHINE) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, **kw: Any) -> Any:
        seen.append((method, path))
        if method == "GET":
            return {"id": "t1", "machine": machine}
        return {**MACHINE, "state": "sleeping" if path.endswith("/sleep") else "gone"}

    monkeypatch.setattr(client, "request", fake_request)
    return seen


def _run(*args: str, stdin: str | None = None) -> Any:
    return CliRunner().invoke(app, list(args), input=stdin)


def test_show_prints_the_machine_block_as_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch)
    r = _run("tasks", "machine", "t1", "--json")
    assert r.exit_code == 0 and json.loads(r.stdout)["state"] == "asleep"


def test_show_human_block_and_no_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch)
    r = _run("tasks", "machine", "t1")
    assert (
        r.exit_code == 0 and "Asleep since" in r.stdout and "340 MB of files and browser logins" in r.stdout
    )
    _fake(monkeypatch, machine=None)
    r = _run("tasks", "machine", "t1")
    assert r.exit_code == 0 and "no Machine yet" in r.stdout


def test_sleep_posts_and_reports_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch)
    r = _run("tasks", "machine", "t1", "--sleep", "--json")
    assert r.exit_code == 0 and ("POST", "/v1/tasks/t1/machine/sleep") in seen
    assert json.loads(r.stdout)["state"] == "sleeping"


def test_reset_asks_first_unless_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch)
    r = _run("tasks", "machine", "t1", "--reset", stdin="n\n")
    assert r.exit_code == 0 and ("POST", "/v1/tasks/t1/machine/reset") not in seen
    r = _run("tasks", "machine", "t1", "--reset", "--yes", "--json")
    assert r.exit_code == 0 and json.loads(r.stdout)["state"] == "gone"


def test_reset_in_json_mode_needs_yes_and_never_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--json` is for scripts - no confirmation prompt on stdout. Without
    `--yes` it refuses, exits non-zero and says why, and nothing is reset."""
    seen = _fake(monkeypatch)
    r = _run("tasks", "machine", "t1", "--reset", "--json", stdin="y\n")
    assert r.exit_code != 0 and ("POST", "/v1/tasks/t1/machine/reset") not in seen
    assert "Reset the Machine?" not in r.stdout
    assert "--yes" in json.loads(r.stderr)["error"]


def test_sleep_and_reset_together_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch)
    assert _run("tasks", "machine", "t1", "--sleep", "--reset").exit_code == 1


def test_tasks_show_carries_a_machine_line(monkeypatch: pytest.MonkeyPatch) -> None:
    task: dict[str, Any] = {
        "id": "t1",
        "status": "done",
        "goal": "g",
        "cost": {"total_usd": "0", "calls": 0},
        "plan": {"steps": []},
        "machine": MACHINE,
    }
    monkeypatch.setattr(client, "request", lambda *a, **k: task)
    assert "  machine: Asleep · browser" in _run("tasks", "show", "t1").stdout
    task["machine"] = None
    assert "machine:" not in _run("tasks", "show", "t1").stdout
