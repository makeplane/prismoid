# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid up`'s supervisor (ADR-077): children in order, each waited for; stopped in reverse; one per home.

The children are small `python -c` scripts that write what they do to a shared trail file, so a test
reads the order things happened in, not just the end state. The rig is `tests/support/rigs/supervisor_rig.py`."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from prismoid.conf import settings
from prismoid.local.supervisor import (
    AlreadyRunning,
    ChildFailed,
    InstanceLock,
    ReadyTimeout,
    Supervisor,
    started,
)
from tests.support.rigs.supervisor_rig import child_mode as child_mode
from tests.support.rigs.supervisor_rig import home as home
from tests.support.rigs.supervisor_rig import interrupt_self, kill_stack, sup_child, sup_trail
from tests.support.rigs.supervisor_rig import no_runtime_build as no_runtime_build
from tests.support.rigs.supervisor_rig import running as running


def test_children_start_in_order_and_wait_ready(home: Path, tmp_path: Path) -> None:
    trail = tmp_path / "trail"
    specs = [sup_child("a", trail, delay=0.5), sup_child("b", trail), sup_child("c", trail)]
    Supervisor(home, specs).up(on_ready=interrupt_self)
    assert sup_trail(trail)[:6] == ["start a", "ready a", "start b", "ready b", "start c", "ready c"]
    for name in ("a", "b", "c"):
        log = home / "run" / f"{name}.log"
        assert log.read_text().splitlines()[0] == "ready"
        assert oct(log.stat().st_mode & 0o777) == "0o600"


def test_sigint_stops_in_reverse(home: Path, tmp_path: Path) -> None:
    trail = tmp_path / "trail"
    specs = [sup_child("a", trail), sup_child("b", trail), sup_child("c", trail)]
    Supervisor(home, specs).up(on_ready=interrupt_self)
    assert [ln for ln in sup_trail(trail) if ln.startswith("stop")] == ["stop c", "stop b", "stop a"]
    assert InstanceLock(home).holder() is None  # the lock went with the supervisor


def test_ready_timeout_stops_started_children(home: Path, tmp_path: Path) -> None:
    trail = tmp_path / "trail"
    specs = [sup_child("a", trail), sup_child("b", trail, delay=999, timeout=1.0), sup_child("c", trail)]
    with pytest.raises(ReadyTimeout) as caught:
        Supervisor(home, specs).up(on_ready=interrupt_self)
    assert caught.value.name == "b"
    lines = sup_trail(trail)
    assert "start c" not in lines
    assert [ln for ln in lines if ln.startswith("stop")] == ["stop b", "stop a"]


def test_child_exit_stops_the_rest_and_names_it(home: Path, tmp_path: Path) -> None:
    trail = tmp_path / "trail"
    specs = [sup_child("a", trail), sup_child("b", trail, then="exit"), sup_child("c", trail)]
    with pytest.raises(ChildFailed) as caught:
        Supervisor(home, specs).up()
    assert caught.value.name == "b"
    assert caught.value.code == 3
    assert "something broke" in caught.value.tail
    assert [ln for ln in sup_trail(trail) if ln.startswith("stop")] == ["stop c", "stop a"]


def test_second_up_attaches(home: Path, tmp_path: Path) -> None:
    first = InstanceLock(home)
    assert first.acquire()
    try:
        trail = tmp_path / "trail"
        with pytest.raises(AlreadyRunning) as caught:
            Supervisor(home, [sup_child("a", trail)]).up(on_ready=interrupt_self)
        assert caught.value.pid == os.getpid()
        assert sup_trail(trail) == []  # nothing started beside the running stack
    finally:
        first.release()


def test_second_up_attaches_from_the_cli(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from prismoid.runner.main import app

    monkeypatch.setattr(settings.local, "home", home)
    monkeypatch.setattr(sys, "platform", "darwin")
    first = InstanceLock(home)
    assert first.acquire()
    try:
        result = CliRunner().invoke(app, ["up", "--json"])
    finally:
        first.release()
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["attached"] is True
    assert out["pid"] == os.getpid()
    assert out["home"] == str(home)


def test_stale_lock_reclaimed(home: Path) -> None:
    run = home / "run"
    run.mkdir(parents=True)
    # A pid file left by a crash. Its pid is even alive (our parent) - a pid is not proof of a holder,
    # the lock is: nobody holds it, so it is stale.
    (run / "prismoid.lock").write_text(f"{os.getppid()}\n")
    lock = InstanceLock(home)
    assert lock.holder() is None
    assert lock.acquire()
    try:
        pid, start, role = (run / "prismoid.lock").read_text().split()
        assert (int(pid), role) == (os.getpid(), "up")
        assert int(start) == started(os.getpid())
        assert oct((run / "prismoid.lock").stat().st_mode & 0o777) == "0o600"
        holder = InstanceLock(home).holder()
        assert holder is not None and holder.pid == os.getpid() and holder.process.alive()
    finally:
        lock.release()


@pytest.mark.parametrize("child_mode", ["deaf"])
def test_a_failed_test_leaves_no_deaf_child_behind(
    home: Path, running: tuple[subprocess.Popen[str], Path], child_mode: str
) -> None:
    proc, _ = running
    stack = kill_stack(home, proc)
    assert len(stack) == 2
    deadline = time.monotonic() + 5
    while any(child.alive() for child in stack) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not any(child.alive() for child in stack)


def test_status_then_down(home: Path, running: tuple[subprocess.Popen[str], Path]) -> None:
    proc, trail = running
    assert Supervisor(home, []).status() == {"a": "running", "b": "running"}  # names from the record
    sup = Supervisor(home, [sup_child("a", trail), sup_child("b", trail)])
    sup.down()
    assert proc.wait(timeout=30) == 0
    assert [ln for ln in sup_trail(trail) if ln.startswith("stop")] == ["stop b", "stop a"]
    assert sup.status() == {"a": "stopped", "b": "stopped"}
    sup.down()  # nothing running: a no-op, never a signal to a stale pid


_HOLD_LOCK = r"""
import sys, time
from pathlib import Path
from prismoid.local.supervisor import InstanceLock
assert InstanceLock(Path(sys.argv[1])).acquire()
print("held", flush=True)
time.sleep(600)
"""


def test_second_helper_refused() -> None:
    """A helper not launched by the supervisor holding the lock would adopt (and SIGKILL) the running
    helper's process groups, so it refuses to start."""
    home = Path(f"/tmp/prismoid-h-{os.getpid()}")  # short: the helper's socket path must fit 104 bytes
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(home)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        r = subprocess.run(
            [sys.executable, "-m", "prismoid.runner.main", "device", "helper", "--home", str(home)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        holder.kill()
        holder.wait()
        if holder.stdout is not None:
            holder.stdout.close()
        subprocess.run(["rm", "-rf", str(home)], check=False)
    assert r.returncode == 1, r.stderr
    assert "already running" in r.stderr
    assert not (home / "run" / "helper.sock").exists()
