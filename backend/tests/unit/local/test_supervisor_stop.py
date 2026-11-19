# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""How `prismoid up` is stopped (ADR-077): a stranger wearing a pid is never signalled, an orphan that
ignores SIGINT is killed, a second Ctrl-C kills what has not stopped, and a failed rebuild stops nothing.
The rig is `tests/support/rigs/supervisor_rig.py`."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from prismoid.conf import settings
from prismoid.local.supervisor import (
    InstanceLock,
    ReadyTimeout,
    Supervisor,
    started,
)
from tests.support.rigs.supervisor_rig import child_mode as child_mode
from tests.support.rigs.supervisor_rig import fake_cluster, fake_prep, sup_trail
from tests.support.rigs.supervisor_rig import home as home
from tests.support.rigs.supervisor_rig import no_runtime_build as no_runtime_build
from tests.support.rigs.supervisor_rig import running as running

_STRANGER = r"""
import signal, sys, time
def hit(*_):
    open(sys.argv[1], "a").write("signalled\n")
for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(s, hit)
print("ready", flush=True)
time.sleep(600)
"""


_ORPHAN = r"""
import fcntl, os, signal, sys, time
os.makedirs(os.path.dirname(sys.argv[1]), exist_ok=True)
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
if sys.argv[2] == "deaf":
    signal.signal(signal.SIGINT, signal.SIG_IGN)
print("ready", flush=True)
time.sleep(600)
"""


def _spawn(script: str, *args: str) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [sys.executable, "-c", script, *args], stdout=subprocess.PIPE, text=True, start_new_session=True
    )
    assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
    return proc


def _reap(*procs: subprocess.Popen[str]) -> None:
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait()
        if p.stdout is not None:
            p.stdout.close()


def _orphaned_stack(
    home: Path, supervisor_pid: int, supervisor_start: int, children: dict[str, list[int]]
) -> None:
    """What a supervisor killed outright leaves: a lock naming it, a state naming its children."""
    run = home / "run"
    (run / "prismoid.lock").write_text(f"{supervisor_pid} {supervisor_start} up\n")  # same inode: still held
    (run / "prismoid.state").write_text(
        json.dumps({"pid": supervisor_pid, "ready": True, "children": children})
    )


def test_down_never_signals_a_stranger_wearing_the_supervisors_pid(home: Path, tmp_path: Path) -> None:
    hits = tmp_path / "hits"
    orphan = _spawn(_ORPHAN, str(home / "run" / "prismoid.lock"), "listening")
    stranger = _spawn(_STRANGER, str(hits))
    try:
        stranger_start = started(stranger.pid)
        assert stranger_start is not None
        # The recorded supervisor's pid now belongs to someone else (its start time does not match).
        _orphaned_stack(
            home, stranger.pid, stranger_start + 1, {"helper": [orphan.pid, started(orphan.pid) or 0]}
        )
        Supervisor(home, []).down(timeout_s=10)
        assert orphan.wait(timeout=10) is not None  # the orphan holding the lock was stopped
        assert not InstanceLock(home).taken()
        assert stranger.poll() is None and not hits.exists()  # the stranger never heard a thing
    finally:
        _reap(orphan, stranger)


def test_down_never_signals_a_stranger_wearing_a_childs_pid(home: Path, tmp_path: Path) -> None:
    hits = tmp_path / "hits"
    orphan = _spawn(_ORPHAN, str(home / "run" / "prismoid.lock"), "listening")
    stranger = _spawn(_STRANGER, str(hits))
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()  # a supervisor that is gone
    try:
        _orphaned_stack(
            home,
            gone.pid,
            1,
            {
                "helper": [orphan.pid, started(orphan.pid) or 0],
                "worker": [stranger.pid, (started(stranger.pid) or 0) + 1],  # pid reused since
            },
        )
        Supervisor(home, []).down(timeout_s=10)
        assert orphan.wait(timeout=10) is not None
        assert not InstanceLock(home).taken()
        assert stranger.poll() is None and not hits.exists()
    finally:
        _reap(orphan, stranger)


def test_down_kills_an_orphan_that_ignores_sigint(home: Path) -> None:
    orphan = _spawn(_ORPHAN, str(home / "run" / "prismoid.lock"), "deaf")
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    try:
        _orphaned_stack(home, gone.pid, 1, {"helper": [orphan.pid, started(orphan.pid) or 0]})
        sup = Supervisor(home, [])
        sup.stop_timeout_s = 1.0
        sup.down(timeout_s=10)
        assert orphan.wait(timeout=10) == -signal.SIGKILL
        assert not InstanceLock(home).taken()
    finally:
        _reap(orphan)


def test_sighup_stops_cleanly_in_reverse(home: Path, running: tuple[subprocess.Popen[str], Path]) -> None:
    """Closing the terminal of a foreground `up` is a clean stop, not a death without cleanup."""
    proc, trail = running
    proc.send_signal(signal.SIGHUP)
    assert proc.wait(timeout=30) == 0
    assert [ln for ln in sup_trail(trail) if ln.startswith("stop")] == ["stop b", "stop a"]
    assert not InstanceLock(home).taken()


@pytest.mark.parametrize("child_mode", ["deaf"])
def test_a_second_ctrl_c_kills_what_has_not_stopped(
    home: Path, running: tuple[subprocess.Popen[str], Path], child_mode: str
) -> None:
    proc, _ = running
    proc.send_signal(signal.SIGINT)
    time.sleep(0.5)
    proc.send_signal(signal.SIGINT)
    assert proc.wait(timeout=5) == 0  # not 2 x 15 s of waiting on children that ignore SIGINT
    assert not InstanceLock(home).taken()


def test_up_beside_a_standalone_helper_says_so(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from prismoid.runner.main import app

    monkeypatch.setattr(settings.local, "home", home)
    monkeypatch.setattr(sys, "platform", "darwin")
    helper = InstanceLock(home)
    assert helper.acquire(role="helper")
    try:
        result = CliRunner().invoke(app, ["up", "--json"])
    finally:
        helper.release()
    assert result.exit_code == 1
    err = json.loads(result.stderr)["error"]
    assert "standalone helper" in err and "prismoid down" in err
    assert "web_url" not in result.stdout


def test_a_failed_postgres_stop_does_not_hide_the_first_error(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismoid.runner import local_cli
    from prismoid.runner.main import app

    fake_prep(monkeypatch, home, tmp_path)
    fake_cluster(monkeypatch, stop_error="pg_ctl stop failed: boom")

    def timed_out(self: Supervisor, on_ready: Any = None) -> None:
        raise ReadyTimeout("worker", 1.0, "the tail", home / "run" / "worker.log")

    monkeypatch.setattr(local_cli.Supervisor, "up", timed_out)
    result = CliRunner().invoke(app, ["up", "--json"])
    assert result.exit_code == 3  # the readiness timeout's code, not the stop's
    first, *rest = result.stderr.splitlines()
    assert "worker was not ready" in json.loads(first)["error"]
    assert any("Postgres did not stop" in ln for ln in rest)


class _Daemon:
    def __init__(self, events: list[str]) -> None:
        self.events, self.pid, self._polls = events, 999_999, 0

    def poll(self) -> int | None:
        self._polls += 1
        if self._polls == 1:
            raise KeyboardInterrupt  # Ctrl-C while `up --background` waits for it
        return 0 if "terminate" in self.events else None

    def terminate(self) -> None:
        self.events.append("terminate")

    def wait(self, timeout: float | None = None) -> int:
        self.events.append("wait")
        return 0


def test_ctrl_c_while_waiting_for_the_background_supervisor_ends_it_first(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismoid.runner import local_cli
    from prismoid.runner.main import app

    fake_prep(monkeypatch, home, tmp_path)
    cluster = fake_cluster(monkeypatch)
    events: list[str] = []
    monkeypatch.setattr(local_cli.subprocess, "Popen", lambda *a, **kw: _Daemon(events))
    monkeypatch.setattr(cluster, "stop", lambda self: events.append("postgres stop"))
    result = CliRunner().invoke(app, ["up", "--background", "--json"])
    assert result.exit_code != 0
    assert events == ["terminate", "wait", "postgres stop"]  # never beneath a live supervisor
    assert not InstanceLock(home).taken()


def test_run_dir_is_made_private_and_status_makes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismoid.runner.main import app

    home = tmp_path / "loose"
    (home / "run").mkdir(parents=True, mode=0o755)
    (home / "run").chmod(0o755)
    lock = InstanceLock(home)
    assert lock.acquire()
    lock.release()
    assert oct((home / "run").stat().st_mode & 0o777) == "0o700"

    fresh = tmp_path / "fresh"
    monkeypatch.setattr(settings.local, "home", fresh)
    result = CliRunner().invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["running"] is False
    assert not fresh.exists()


class _SlowDaemon(_Daemon):
    """Ctrl-C again while `up --background` waits for the supervisor it is stopping."""

    def __init__(self, events: list[str], *, hangs: bool = False) -> None:
        super().__init__(events)
        self.hangs = hangs

    def wait(self, timeout: float | None = None) -> int:
        self.events.append("wait")
        if self.hangs and timeout is not None:
            raise subprocess.TimeoutExpired("prismoid up", timeout)
        os.kill(os.getpid(), signal.SIGINT)  # the third Ctrl-C
        time.sleep(0.05)  # where a handler would raise
        self.events.append("waited")
        return 0

    def kill(self) -> None:
        self.events.append("kill")


@pytest.mark.parametrize("hangs", [False, True])
def test_more_ctrl_c_while_the_background_supervisor_stops_never_stops_postgres_beneath_it(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hangs: bool
) -> None:
    from prismoid.runner import local_cli
    from prismoid.runner.main import app

    fake_prep(monkeypatch, home, tmp_path)
    cluster = fake_cluster(monkeypatch)
    events: list[str] = []
    monkeypatch.setattr(local_cli.subprocess, "Popen", lambda *a, **kw: _SlowDaemon(events, hangs=hangs))
    monkeypatch.setattr(cluster, "stop", lambda self: events.append("postgres stop"))
    # What this process had, not the default: a uvicorn served in an earlier test's fixture restores
    # the asyncio runner's handler it found, and that outlives the runner.
    before = signal.getsignal(signal.SIGINT)
    result = CliRunner().invoke(app, ["up", "--background", "--json"])
    assert result.exit_code != 0
    if hangs:  # killed before it had stopped: what it started may live on, so Postgres stays
        assert events == ["terminate", "wait", "kill", "wait", "waited"]
        assert "prismoid down" in result.stderr
    else:
        assert events == ["terminate", "wait", "waited", "postgres stop"]
    assert signal.getsignal(signal.SIGINT) is before  # Ctrl-C works again


def test_a_failed_runtime_build_stops_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Offline at the first `up`: one clear line, and the rest starts."""
    from prismoid.local.runtime import RuntimeBuildFailed
    from prismoid.runner import local_cli

    def offline(home: Path, *, building: Any) -> Path:
        building()
        raise RuntimeBuildFailed("uv failed: no network")

    monkeypatch.setattr(local_cli, "ensure_runtime", offline)
    local_cli._prepare_runtime(  # pyright: ignore[reportPrivateUsage]
        tmp_path, {"PRISMOID_LOCAL_HAS_OFFICE": "true", "PRISMOID_LOCAL_HAS_POPPLER": "true"}
    )
    err = capsys.readouterr().err
    assert "was not built" in err and "will not publish" in err and "uv failed: no network" in err
    assert "first run" not in err


def test_a_failed_rebuild_says_the_runtime_here_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from prismoid.local.runtime import RuntimeBuildFailed
    from prismoid.runner import local_cli

    def offline(home: Path, *, building: Any) -> Path:
        raise RuntimeBuildFailed("uv failed: no network")

    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "stamp").write_text("older")
    monkeypatch.setattr(local_cli, "ensure_runtime", offline)
    env = {"PRISMOID_LOCAL_HAS_OFFICE": "true", "PRISMOID_LOCAL_HAS_POPPLER": "true"}
    local_cli._prepare_runtime(tmp_path, env)  # pyright: ignore[reportPrivateUsage]
    err = capsys.readouterr().err
    assert "will not publish" in err, "a stamp over a dead interpreter is no runtime"

    python = tmp_path / "runtime" / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    local_cli._prepare_runtime(tmp_path, env)  # pyright: ignore[reportPrivateUsage]
    err = capsys.readouterr().err
    assert "the one here stays" in err and "will not publish" not in err


def test_the_children_are_told_what_the_preflight_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismoid.local.postgres import PgUrls
    from prismoid.runner import local_cli

    monkeypatch.setattr(
        local_cli,
        "preflight",
        lambda: {"PRISMOID_LOCAL_HAS_OFFICE": "false", "PRISMOID_LOCAL_HAS_POPPLER": "true"},
    )
    env = local_cli.child_env(tmp_path, PgUrls(async_url="a", sync_url="s"), tmp_path, 9123)
    assert env["PRISMOID_LOCAL_HAS_POPPLER"] == "true"
    assert local_cli.helper_env(env)["PRISMOID_LOCAL_HAS_POPPLER"] == "true"
