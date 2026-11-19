# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What `prismoid up` checks and hands its children (ADR-077): the model key before the worker, the
environment each child gets, and the data home they run in. The rig is `tests/support/rigs/supervisor_rig.py`."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from prismoid.local.supervisor import (
    ChildSpec,
    InstanceLock,
    Supervisor,
)
from tests.support.rigs.supervisor_rig import fake_cluster, fake_prep, interrupt_self
from tests.support.rigs.supervisor_rig import home as home
from tests.support.rigs.supervisor_rig import no_runtime_build as no_runtime_build


def test_missing_model_key_fails_before_worker(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismoid.runner import local_cli
    from prismoid.runner.main import app

    supervised: list[str] = []
    fake_prep(monkeypatch, home, tmp_path)
    cluster = fake_cluster(monkeypatch)
    missing = [
        {"check": "key-missing", "ref": ref, "detail": "no key"}
        for ref in ("anthropic:claude-x", "openai:gpt-x")
    ]
    monkeypatch.setattr(
        local_cli, "_catalog_health", lambda: (["anthropic:claude-x", "openai:gpt-x"], missing)
    )
    monkeypatch.setattr(local_cli.Supervisor, "up", lambda self, on_ready=None: supervised.append("up"))
    result = CliRunner().invoke(app, ["up", "--json"])
    assert result.exit_code == 1
    assert supervised == []  # the worker never started
    err = json.loads(result.stderr)["error"]
    assert "PRISMOID_WORKER_ANTHROPIC_API_KEY" in err
    assert "PRISMOID_WORKER_OPENAI_API_KEY" in err
    assert "~/.prismoid/config" in err or "prismoid-config" in err  # the suite points the file elsewhere
    assert [c.stopped for c in cluster.made] == [True]  # Postgres goes down with the failed start
    assert InstanceLock(home).holder() is None


def test_one_keyed_model_is_enough() -> None:
    from prismoid.runner.local_cli import model_key_error

    missing = [{"check": "key-missing", "ref": "openai:gpt-x", "detail": ""}]
    assert model_key_error(["anthropic:claude-x", "openai:gpt-x"], missing) is None
    assert model_key_error(["openai:gpt-x"], missing) is not None


def test_child_env_is_local_on_the_cluster_and_never_carries_the_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from prismoid.local.postgres import PgUrls
    from prismoid.runner.local_cli import child_env

    monkeypatch.setenv("PRISMOID_LOCAL_OWNER_PASSWORD", "a-long-enough-password")
    monkeypatch.setenv("PRISMOID_ENVIRONMENT", "dev")
    monkeypatch.setenv("PRISMOID_WORKER_FRAME_DRIVER", "docker")
    urls = PgUrls(async_url="postgresql+asyncpg://a@/prismoid", sync_url="postgresql://a@/prismoid")
    env = child_env(tmp_path, urls, tmp_path / "web", 9123)
    assert "PRISMOID_LOCAL_OWNER_PASSWORD" not in env
    assert "PRISMOID_ENVIRONMENT" not in env
    assert env["PRISMOID_ENV"] == "local"
    assert env["PRISMOID_WORKER_FRAME_DRIVER"] == "device"
    assert env["PRISMOID_WORKER_STORAGE_BACKEND"] == "fs"
    assert (env["PRISMOID_API_DATABASE_URL"], env["PRISMOID_WORKER_DATABASE_URL"]) == (
        urls.async_url,
        urls.sync_url,
    )
    assert env["PRISMOID_API_HOST"] == "127.0.0.1"
    assert env["PRISMOID_API_PUBLIC_BASE_URL"] == "http://127.0.0.1:9123"
    assert env["PRISMOID_LOCAL_WEB_DIR"] == str(tmp_path / "web")


def _env_with(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config: str = "") -> dict[str, str]:
    """`child_env` with `config` as the user's `~/.prismoid/config`, and no repo `.env` to speak."""
    from prismoid.conf import ComputerUseSettings
    from prismoid.local.postgres import PgUrls
    from prismoid.runner.local_cli import child_env

    (tmp_path / "config").write_text(config)
    monkeypatch.setitem(ComputerUseSettings.model_config, "env_file", (str(tmp_path / "config"),))
    urls = PgUrls(async_url="postgresql+asyncpg://a@/prismoid", sync_url="postgresql://a@/prismoid")
    return child_env(tmp_path, urls, tmp_path / "web", 9123)


def test_child_env_turns_computer_use_on_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The coding seat's browser (spec O11): nobody said otherwise, so it is on."""
    monkeypatch.delenv("PRISMOID_COMPUTER_USE_ENABLED", raising=False)
    assert _env_with(monkeypatch, tmp_path)["PRISMOID_COMPUTER_USE_ENABLED"] == "true"


def test_the_kill_switch_in_the_environment_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PRISMOID_COMPUTER_USE_ENABLED", "false")
    assert _env_with(monkeypatch, tmp_path)["PRISMOID_COMPUTER_USE_ENABLED"] == "false"


def test_the_kill_switch_in_the_users_config_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The child reads the file itself, so `up` must not set the variable over it: a real
    environment variable beats the file."""
    from prismoid.conf import ComputerUseSettings

    monkeypatch.delenv("PRISMOID_COMPUTER_USE_ENABLED", raising=False)
    env = _env_with(monkeypatch, tmp_path, "PRISMOID_COMPUTER_USE_ENABLED=false\n")
    assert "PRISMOID_COMPUTER_USE_ENABLED" not in env
    assert ComputerUseSettings().enabled is False


def test_the_note_says_coding_tasks_need_chrome() -> None:
    from prismoid.device.chrome import missing

    assert "coding" in (missing("/no/such/chrome") or "")


def test_postgres_tools_see_no_secret(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`pg_ctl start` hands its environment to the postmaster for life: no password, no model key."""
    from prismoid.local import postgres

    monkeypatch.setenv("PRISMOID_LOCAL_OWNER_PASSWORD", "a-long-enough-password")
    monkeypatch.setenv("PRISMOID_WORKER_ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("PRISMOID_API_DATABASE_URL", "postgresql+asyncpg://prismoid:pw@/prismoid")
    monkeypatch.setenv("LC_ALL", "C")
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(postgres.Cluster, "_bin", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(
        postgres.subprocess,
        "run",
        lambda *a, **kw: seen.append(kw) or subprocess.CompletedProcess(a, 0, "", ""),
    )
    (tmp_path / "postgres").mkdir()
    (tmp_path / "postgres" / "PG_VERSION").write_text("18")
    postgres.Cluster(tmp_path).stop()
    env = seen[0]["env"]
    assert env is not None
    assert not [k for k in env if k.startswith("PRISMOID_") or k.endswith("_API_KEY")]
    assert env["PATH"] == os.environ["PATH"]
    assert env["LC_ALL"] == "C"


def test_up_takes_the_owner_answers_out_of_the_environment(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismoid.runner import local_cli
    from prismoid.runner.main import app

    fake_prep(monkeypatch, home, tmp_path)
    fake_cluster(monkeypatch)
    monkeypatch.setenv("PRISMOID_LOCAL_OWNER_PASSWORD", "a-long-enough-password")
    monkeypatch.setenv("PRISMOID_LOCAL_OWNER_EMAIL", "me@example.com")
    seen: list[tuple[dict[str, str], str | None]] = []

    def owner(json_out: bool, web_url: str, answers: dict[str, str]) -> bool:
        seen.append((answers, os.environ.get("PRISMOID_LOCAL_OWNER_PASSWORD")))
        return False

    monkeypatch.setattr(local_cli, "_ensure_owner", owner)
    monkeypatch.setattr(local_cli.Supervisor, "up", lambda self, on_ready=None: None)
    result = CliRunner().invoke(app, ["up", "--json"])
    assert result.exit_code == 0, result.output
    answers, left_in_env = seen[0]
    assert answers["PRISMOID_LOCAL_OWNER_PASSWORD"] == "a-long-enough-password"
    assert left_in_env is None  # gone before Postgres or any child could inherit it


def test_a_second_ctrl_c_never_cuts_the_helpers_stop_short(tmp_path: Path) -> None:
    """Its commands are in sessions of their own: killing the helper early orphans them."""
    from prismoid.local.postgres import PgUrls
    from prismoid.runner.local_cli import child_env, child_specs

    urls = PgUrls(async_url="a", sync_url="s")
    specs = child_specs(tmp_path, child_env(tmp_path, urls, tmp_path, 8100), 8100)
    assert {s.name: s.keeps_its_stop for s in specs} == {"helper": True, "worker": False, "api": False}


def test_helper_starts_first_with_a_bare_environment(tmp_path: Path) -> None:
    """R1: helper, worker, API (so stopping is API, worker, helper). M1: the helper never sees a
    model key or the database."""
    from prismoid.local.postgres import PgUrls
    from prismoid.runner.local_cli import child_env, child_specs

    urls = PgUrls(async_url="postgresql+asyncpg://a:pw@/prismoid", sync_url="postgresql://a:pw@/prismoid")
    env = {
        **child_env(tmp_path, urls, tmp_path / "web", 9123),
        "PRISMOID_WORKER_ANTHROPIC_API_KEY": "sk-ant-secret",
    }
    specs = child_specs(tmp_path, env, 9123)
    assert [s.name for s in specs] == ["helper", "worker", "api"]
    helper, worker = specs[0].env, specs[1].env
    assert not [k for k in helper if k.endswith("_API_KEY") or k.endswith("DATABASE_URL")]
    assert helper["PRISMOID_LOCAL_HOME"] == str(tmp_path)
    assert helper["PATH"] == env["PATH"]
    assert worker["PRISMOID_WORKER_ANTHROPIC_API_KEY"] == "sk-ant-secret"


def test_children_run_in_the_data_home(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # wherever `up` was typed, with its own `.env`
    spec = ChildSpec(
        name="where",
        argv=[sys.executable, "-c", "import os, time; print(os.getcwd(), flush=True); time.sleep(60)"],
        env=dict(os.environ),
        ready=lambda line: line is not None,
    )
    Supervisor(home, [spec]).up(on_ready=interrupt_self)
    assert Path((home / "run" / "where.log").read_text().split()[0]).resolve() == home.resolve()
