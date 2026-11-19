# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What `prismoid up` and `prismoid down` say at the door (ADR-077): a hint that signs in to the stack just
started, and a Postgres that would not move as an error document, never a traceback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from prismoid.conf import CliSettings
from prismoid.local.supervisor import InstanceLock, Supervisor
from prismoid.runner import client
from tests.support.rigs.supervisor_rig import fake_cluster, fake_prep
from tests.support.rigs.supervisor_rig import home as home
from tests.support.rigs.supervisor_rig import no_runtime_build as no_runtime_build
from tests.support.settings_env import no_developer_settings


def _up_needing_a_sign_in(monkeypatch: pytest.MonkeyPatch, home: Path, tmp_path: Path) -> None:
    from prismoid.runner import local_cli

    fake_prep(monkeypatch, home, tmp_path)
    fake_cluster(monkeypatch)
    monkeypatch.setattr(local_cli, "_ensure_owner", lambda json_out, web_url, owner: True)

    def ready_at_once(self: Supervisor, on_ready: object = None) -> None:
        assert callable(on_ready)
        on_ready()

    monkeypatch.setattr(local_cli.Supervisor, "up", ready_at_once)


def test_the_json_hint_signs_in_to_the_stack_just_started(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismoid.runner.main import app

    _up_needing_a_sign_in(monkeypatch, home, tmp_path)
    result = CliRunner().invoke(app, ["up", "--json"])
    assert result.exit_code == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["sign_in"] == f"PRISMOID_CLI_API_URL={data['web_url']} prismoid login"


def test_the_printed_hint_signs_in_to_the_stack_just_started(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismoid.conf import settings
    from prismoid.runner.main import app

    _up_needing_a_sign_in(monkeypatch, home, tmp_path)
    result = CliRunner().invoke(app, ["up"])
    assert result.exit_code == 0, result.stderr
    web_url = f"http://127.0.0.1:{settings.local.api_port}"
    assert f"Sign this terminal in with: PRISMOID_CLI_API_URL={web_url} prismoid login" in result.stdout


def test_the_hints_address_beats_the_one_last_signed_in_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why the hint names the address: `prismoid login` alone goes to the other Prismoid this terminal knows."""
    no_developer_settings(monkeypatch, tmp_path)  # no configured address: only the signed-in one
    monkeypatch.setattr(client.settings, "cli", CliSettings())
    monkeypatch.setattr(client, "CREDENTIALS", tmp_path / "credentials")
    client.save_token("a-dev-stack-token", "http://localhost:8100")
    assert client.api_url() == "http://localhost:8100"
    monkeypatch.setenv("PRISMOID_CLI_API_URL", "http://127.0.0.1:8321")
    assert client.api_url() == "http://127.0.0.1:8321"


def test_a_postgres_that_will_not_start_is_an_error_document(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismoid.runner.main import app

    fake_prep(monkeypatch, home, tmp_path)
    cluster = fake_cluster(monkeypatch)

    def refuses(self: object) -> None:
        raise RuntimeError("pg_ctl start failed: boom")

    monkeypatch.setattr(cluster, "start", refuses)
    result = CliRunner().invoke(app, ["up", "--json"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)  # no traceback: typer's own exit
    assert json.loads(result.stderr.splitlines()[0])["error"] == "pg_ctl start failed: boom"


def test_a_postgres_that_will_not_initialise_is_an_error_document(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismoid.runner.main import app

    fake_prep(monkeypatch, home, tmp_path)
    cluster = fake_cluster(monkeypatch)

    def refuses(self: object, password: str) -> None:
        raise RuntimeError("initdb failed: no space left on device")

    monkeypatch.setattr(cluster, "ensure", refuses)
    result = CliRunner().invoke(app, ["up", "--json"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "initdb failed" in json.loads(result.stderr.splitlines()[0])["error"]


def test_a_postgres_that_will_not_stop_is_an_error_document_from_down(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismoid.runner.main import app

    fake_prep(monkeypatch, home, tmp_path)
    cluster = fake_cluster(monkeypatch, stop_error="pg_ctl stop failed: boom")
    monkeypatch.setattr(cluster, "running", lambda self: True, raising=False)
    monkeypatch.setattr(InstanceLock, "taken", lambda self: True)
    monkeypatch.setattr(Supervisor, "down", lambda self, timeout_s=0.0: None)
    result = CliRunner().invoke(app, ["down", "--json"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "pg_ctl stop failed: boom" in json.loads(result.stderr)["error"]
    again = InstanceLock(home)
    assert again.acquire(), "the lock went back"
    again.release()
