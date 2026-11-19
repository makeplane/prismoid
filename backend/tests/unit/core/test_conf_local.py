# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`PRISMOID_ENV=local` (ADR-077): its two requirements, and the user's config file."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from prismoid.conf import LocalSettings, Settings
from prismoid.conf_env import USER_CONFIG, env_files
from tests.support.hosted import REAL_SECRETS


def test_local_env_requires_device_and_fs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRISMOID_ENV", "local")
    monkeypatch.delenv("PRISMOID_WORKER_FRAME_DRIVER", raising=False)
    monkeypatch.delenv("PRISMOID_WORKER_STORAGE_BACKEND", raising=False)
    with pytest.raises(ValidationError, match="PRISMOID_ENV=local requires"):
        Settings()
    monkeypatch.setenv("PRISMOID_WORKER_FRAME_DRIVER", "device")
    with pytest.raises(ValidationError, match="PRISMOID_ENV=local requires"):
        Settings()
    monkeypatch.setenv("PRISMOID_WORKER_STORAGE_BACKEND", "fs")
    ok = Settings()
    assert ok.environment == "local" and ok.email.backend == "console"


def test_other_environments_keep_their_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRISMOID_ENV", "dev")
    assert Settings().worker.storage_backend == "s3"


def _use_config_file(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setitem(LocalSettings.model_config, "env_file", (".env", str(path)))


@pytest.mark.parametrize("name", ["PRISMOID_ENV", "PRISMOID_ENVIRONMENT"])
@pytest.mark.parametrize("env", ["staging", "prod"])
def test_a_deployed_service_reads_no_user_config(name: str, env: str) -> None:
    assert env_files({name: env}) == (".env",)


@pytest.mark.parametrize(
    "env", [{}, {"PRISMOID_ENV": "dev"}, {"PRISMOID_ENV": "local"}, {"PRISMOID_ENV": "test"}]
)
def test_everything_else_reads_it(env: dict[str, str]) -> None:
    assert env_files(env) == (".env", USER_CONFIG)


@pytest.mark.parametrize(("env", "read"), [("staging", "24"), ("dev", "7")])
def test_the_gate_holds_at_import(tmp_path: Path, env: str, read: str) -> None:
    """The real read, in a process of its own: `prismoid.conf` builds its settings as it is imported."""
    config = tmp_path / "config"
    config.write_text("PRISMOID_LOCAL_AWAY_LIMIT_H=7\n")
    done = subprocess.run(
        [sys.executable, "-c", "from prismoid.conf import settings; print(settings.local.away_limit_h)"],
        env={
            "PATH": os.environ["PATH"],
            "PRISMOID_CONFIG_FILE": str(config),
            "PRISMOID_ENV": env,
            "PRISMOID_EMAIL_BACKEND": "smtp",
            "PRISMOID_COMPUTER_USE_ENABLED": "false",
            **REAL_SECRETS,
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr[-800:]
    assert done.stdout.strip() == read


def test_every_group_reads_the_user_config_after_dotenv() -> None:
    for group in (Settings, LocalSettings):
        assert group.model_config.get("env_file") == (".env", USER_CONFIG)


def test_config_file_is_read_after_dotenv_and_env_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "config"
    config.write_text("PRISMOID_LOCAL_API_PORT=8222\nPRISMOID_LOCAL_AWAY_LIMIT_H=7\n")
    dotenv = tmp_path / ".env"
    dotenv.write_text("PRISMOID_LOCAL_API_PORT=8111\nPRISMOID_LOCAL_AWAY_LIMIT_H=3\n")
    monkeypatch.setitem(LocalSettings.model_config, "env_file", (str(dotenv), str(config)))
    monkeypatch.delenv("PRISMOID_LOCAL_API_PORT", raising=False)
    monkeypatch.delenv("PRISMOID_LOCAL_AWAY_LIMIT_H", raising=False)
    read = LocalSettings()
    assert read.api_port == 8222 and read.away_limit_h == 7  # the config file beats .env
    monkeypatch.setenv("PRISMOID_LOCAL_API_PORT", "8333")
    assert LocalSettings().api_port == 8333  # a real environment variable beats both


def test_a_missing_config_file_is_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_config_file(monkeypatch, tmp_path / "absent")
    monkeypatch.delenv("PRISMOID_LOCAL_API_PORT", raising=False)
    assert LocalSettings().api_port == 8100


def test_the_suite_does_not_read_the_developers_config() -> None:
    assert USER_CONFIG == "/nonexistent/prismoid-config"


@pytest.mark.parametrize("env", ["staging", "prod", "dev", "test"])
def test_fs_storage_is_refused_outside_local(monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    monkeypatch.setenv("PRISMOID_ENV", env)
    monkeypatch.setenv("PRISMOID_WORKER_STORAGE_BACKEND", "fs")
    monkeypatch.setenv("PRISMOID_EMAIL_BACKEND", "smtp")
    if env in ("staging", "prod"):
        with pytest.raises(ValidationError, match="local mode only"):
            Settings()
    else:
        assert Settings().worker.storage_backend == "fs"  # dev/test may exercise it; the door stays shut
