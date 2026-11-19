# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid tasks fork` - the CLI's fork must send the body the API's `ForkIn` requires."""

from __future__ import annotations

import json
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from prismoid.api.serializers.tasks import ForkIn
from prismoid.runner import client
from prismoid.runner.main import app
from prismoid.schemas.device import DOCKER_NOTE


def test_fork_sends_a_body_the_api_accepts(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, str, Any]] = []

    def fake_request(method: str, path: str, **kw: Any) -> Any:
        sent.append((method, path, kw.get("body")))
        return {"task_id": "child"}

    monkeypatch.setattr(client, "request", fake_request)
    result = CliRunner().invoke(app, ["tasks", "fork", "parent"])

    assert result.exit_code == 0, result.output
    [(method, path, body)] = sent
    assert (method, path) == ("POST", "/v1/tasks/parent/fork")
    assert body is not None, "a POST with no body is refused: `validation_failed` (found live, 1b's real run)"
    ForkIn.model_validate(body)


def test_fork_names_its_folder_only_when_given(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """A fork of a task on the device never copies its folder: it is sent
    one only when the person names it, never the cwd."""
    sent: list[Any] = []

    def fake_request(method: str, path: str, **kw: Any) -> Any:
        sent.append(kw.get("body"))
        return {"task_id": "child"}

    monkeypatch.setattr(client, "request", fake_request)
    monkeypatch.chdir(tmp_path)
    assert CliRunner().invoke(app, ["tasks", "fork", "parent"]).exit_code == 0
    assert CliRunner().invoke(app, ["tasks", "fork", "parent", "--folder", str(tmp_path)]).exit_code == 0
    assert sent == [{}, {"folder": str(tmp_path)}]
    ForkIn.model_validate(sent[1])


def _refused_folder(sent: list[dict[str, Any]], told: str) -> Any:
    def fake_request(method: str, path: str, **kw: Any) -> Any:
        if path == "/healthz":
            return {"device": True}
        sent.append(kw)
        raise client.Invalid(told)

    return fake_request


def test_do_from_a_folder_it_cannot_default_to_says_what_to_do(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """`prismoid do` typed in the home folder: the server's refusal, and the two ways out."""
    sent: list[dict[str, Any]] = []
    told = f"folder_refused: {tmp_path} holds the home folder"
    monkeypatch.setattr(client, "request", _refused_folder(sent, told))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["do", "fix the login page", "--no-wait"])
    assert result.exit_code != 0 and sent[0]["raise_invalid"] is True
    assert "holds the home folder" in result.stderr
    assert "cd into a project folder" in result.stderr and "--folder" in result.stderr


def test_a_named_folder_refused_is_the_servers_word_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        client, "request", lambda *a, **kw: {"device": True} if a[1] == "/healthz" else sent.append(kw)
    )
    result = CliRunner().invoke(app, ["do", "x", "--no-wait", "--folder", str(tmp_path)])
    assert sent[0]["raise_invalid"] is False, "a folder they named is theirs to read about"
    assert "cd into" not in result.output


def _accepted(monkeypatch: pytest.MonkeyPatch, order: list[str]) -> None:
    def fake_request(method: str, path: str, **kw: Any) -> Any:
        order.append(path)
        return {"device": True} if path == "/healthz" else {"task_id": "t"}

    monkeypatch.setattr(client, "request", fake_request)


def test_the_docker_grant_says_on_stderr_what_it_does_not_fence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Spec 6.7 and 7.1: the grant's prompt says so in plain words, before the task starts. Stderr,
    so `--json` stays a document."""
    order: list[str] = []
    _accepted(monkeypatch, order)
    real_echo = typer.echo

    def echo(message: str, **kw: Any) -> None:
        order.append(message)
        real_echo(message, **kw)

    monkeypatch.setattr(typer, "echo", echo)
    args = ["do", "x", "--no-wait", "--folder", str(tmp_path), "--docker", "--json"]
    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, result.output
    assert result.stderr.strip() == DOCKER_NOTE and "not fenced" in DOCKER_NOTE
    assert json.loads(result.stdout) == {"task_id": "t"}
    assert order.index(DOCKER_NOTE) < order.index("/v1/tasks"), "before the task starts"


def test_no_docker_grant_says_nothing_of_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    _accepted(monkeypatch, [])
    result = CliRunner().invoke(app, ["do", "x", "--no-wait", "--folder", str(tmp_path), "--json"])
    assert result.exit_code == 0 and result.stderr == ""
