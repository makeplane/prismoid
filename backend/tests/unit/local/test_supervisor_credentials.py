# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The CLI's stored tokens, beside `prismoid up`'s run dir (ADR-077): a token kept per server, owner-only
files, the older shapes still read, and a crash mid-write that loses none. The rig is
`tests/support/rigs/supervisor_rig.py`."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from prismoid.conf import settings
from tests.support.rigs.supervisor_rig import child_mode as child_mode
from tests.support.rigs.supervisor_rig import home as home
from tests.support.rigs.supervisor_rig import no_runtime_build as no_runtime_build
from tests.support.rigs.supervisor_rig import running as running
from tests.support.rigs.supervisor_rig import sup_trail


@pytest.fixture
def credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from pydantic import SecretStr

    from prismoid.conf import CliSettings
    from prismoid.runner import client

    path = tmp_path / "dot-prismoid" / "credentials"
    monkeypatch.setattr(client, "CREDENTIALS", path)
    monkeypatch.delenv("PRISMOID_CLI_API_URL", raising=False)
    monkeypatch.delenv("PRISMOID_CLI_API_KEY", raising=False)
    monkeypatch.delenv("PRISMOID_API_KEY", raising=False)
    cli = CliSettings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert cli.model_fields_set == set() and cli.api_key == SecretStr("")  # nothing configured
    monkeypatch.setattr(settings, "cli", cli)
    return path


def test_a_token_is_kept_per_server_and_another_servers_survives(
    credentials: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismoid.runner import client

    credentials.parent.mkdir()
    credentials.write_text(json.dumps({"token": "cs_dev", "api_url": "http://localhost:8100"}))  # older shape
    assert client.current_token() == "cs_dev"
    client.save_token("ck_local", "http://127.0.0.1:9123")  # `prismoid up`'s first run
    assert client.api_url() == "http://127.0.0.1:9123"  # the address signed in to last is the one in use
    assert client.current_token() == "ck_local"
    assert client.token_for("http://localhost:8100") == "cs_dev"  # the dev stack's token survived
    monkeypatch.setenv("PRISMOID_CLI_API_URL", "http://localhost:8100")  # the environment still wins
    assert client.current_token() == "cs_dev"
    monkeypatch.setenv("PRISMOID_CLI_API_URL", "http://127.0.0.1:5555")
    assert client.current_token() is None  # no server is handed another one's token
    monkeypatch.delenv("PRISMOID_CLI_API_URL")
    client.clear_token()  # logout forgets the address in use only
    assert client.token_for("http://127.0.0.1:9123") is None
    assert client.token_for("http://localhost:8100") == "cs_dev"


def test_credentials_are_born_owner_only(credentials: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from prismoid.runner import client

    monkeypatch.setattr(Path, "chmod", lambda self, mode: None)  # no fixing it up afterwards
    old = os.umask(0o022)
    try:
        client.save_token("ck_local", "http://127.0.0.1:9123")
    finally:
        os.umask(old)
    assert oct(credentials.stat().st_mode & 0o777) == "0o600"


def test_the_login_hint_shows_when_only_another_server_has_a_token(
    credentials: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismoid.runner import client, local_cli

    monkeypatch.setattr(local_cli, "_in_session", lambda work: False)  # the owner exists already
    client.save_token("cs_dev", "http://localhost:8100")
    assert local_cli._ensure_owner(True, "http://127.0.0.1:9123", {}) is True  # pyright: ignore[reportPrivateUsage]
    client.save_token("ck_local", "http://127.0.0.1:9123")
    assert local_cli._ensure_owner(True, "http://127.0.0.1:9123", {}) is False  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("child_mode", ["slow"])
def test_a_closing_terminals_repeated_sighup_still_stops_cleanly(
    home: Path, running: tuple[subprocess.Popen[str], Path], child_mode: str
) -> None:
    """Only a second Ctrl-C escalates: SIGHUP twice (a terminal closing) is still a clean stop."""
    proc, trail = running
    proc.send_signal(signal.SIGHUP)
    time.sleep(0.2)
    proc.send_signal(signal.SIGHUP)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 0
    assert [ln for ln in sup_trail(trail) if ln.startswith("stop")] == ["stop b", "stop a"]


def _raw(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def test_older_clis_keep_reading_the_token_they_know(credentials: Path) -> None:
    """Another worktree's CLI reads only `token`/`api_url`: a save for another address leaves that
    pair exactly as it was."""
    from prismoid.runner import client

    credentials.parent.mkdir()
    credentials.write_text(json.dumps({"token": "cs_dev", "api_url": "http://localhost:8100/"}))
    client.save_token("ck_local", "http://127.0.0.1:9123")
    raw = _raw(credentials)
    assert (raw["token"], raw["api_url"]) == ("cs_dev", "http://localhost:8100/")  # untouched
    assert raw["active"] == "http://127.0.0.1:9123"
    assert raw["tokens"] == {"http://localhost:8100": "cs_dev", "http://127.0.0.1:9123": "ck_local"}
    client.clear_token()  # logout of the local Prismoid: the older pair names another address and stays
    raw = _raw(credentials)
    assert (raw["token"], raw["api_url"]) == ("cs_dev", "http://localhost:8100/")
    assert "http://127.0.0.1:9123" not in raw["tokens"]


def test_the_older_pair_follows_its_own_address(credentials: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from prismoid.runner import client

    client.save_token("ck_one", "http://127.0.0.1:9123")  # nothing before: the pair is written
    raw = _raw(credentials)
    assert (raw["token"], raw["api_url"]) == ("ck_one", "http://127.0.0.1:9123")
    client.save_token("ck_two", "http://127.0.0.1:9123/")  # the same address: the pair moves with it
    raw = _raw(credentials)
    assert (raw["token"], raw["api_url"]) == ("ck_two", "http://127.0.0.1:9123")
    client.clear_token()  # it names the address cleared: it goes too, and so does the file
    assert not credentials.exists()


def test_the_older_shape_alone_is_read(credentials: Path) -> None:
    from prismoid.runner import client

    credentials.parent.mkdir()
    credentials.write_text(json.dumps({"token": "cs_dev", "api_url": "http://localhost:9999"}))
    assert client.api_url() == "http://localhost:9999"
    assert client.current_token() == "cs_dev"
    assert client.token_for("http://localhost:9999/") == "cs_dev"


def test_a_crash_mid_write_loses_no_token(credentials: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from prismoid.runner import client

    client.save_token("cs_dev", "http://localhost:8100")
    before = credentials.read_text()

    def crash(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", crash)
    with pytest.raises(OSError, match="disk full"):
        client.save_token("ck_local", "http://127.0.0.1:9123")
    assert credentials.read_text() == before  # replaced whole or not at all
    assert sorted(p.name for p in credentials.parent.iterdir()) == ["credentials"]  # no temp left


def test_a_configured_address_beats_the_stored_one_which_beats_the_default(
    credentials: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from prismoid.conf import CliSettings
    from prismoid.runner import client

    assert client.api_url() == "http://localhost:8100"  # nothing stored, nothing configured
    client.save_token("ck_local", "http://127.0.0.1:9123")
    assert client.api_url() == "http://127.0.0.1:9123"  # the stored address beats the default
    dotenv = tmp_path / "dev.env"
    dotenv.write_text("PRISMOID_CLI_API_URL=http://localhost:8200\n")  # a dev worktree's `.env`
    monkeypatch.setattr(settings, "cli", CliSettings(_env_file=dotenv))  # pyright: ignore[reportCallIssue]
    assert "PRISMOID_CLI_API_URL" not in os.environ
    assert client.api_url() == "http://localhost:8200"  # configured, so it wins
