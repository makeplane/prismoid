# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The rig for the supervisor tests (ADR-077), shared by the `test_supervisor*` files: children that are
small `python -c` scripts writing what they do to a shared trail file, so a test reads the order things
happened in. A plain module, not a test file."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from prismoid.conf import settings
from prismoid.local.supervisor import (
    ChildSpec,
    Process,
)

_CHILD = r"""
import signal, sys, time
name, trail, delay, then = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
def note(word):
    with open(trail, "a") as f:
        f.write(f"{word} {name}\n")
def stop(*_):
    if then == "slow":
        time.sleep(1.0)  # winds down for a while, as the worker does
    note("stop")
    sys.exit(0)
signal.signal(signal.SIGINT, signal.SIG_IGN if then == "deaf" else stop)
note("start")
time.sleep(delay)
note("ready")
print("ready", flush=True)
if then == "exit":
    time.sleep(0.5)  # the children after this one are up by now
    print("something broke", flush=True)
    sys.exit(3)
while True:
    time.sleep(0.05)
"""


def sup_child(
    name: str, trail: Path, *, delay: float = 0.0, then: str = "wait", timeout: float = 10.0
) -> ChildSpec:
    return ChildSpec(
        name=name,
        argv=[sys.executable, "-c", _CHILD, name, str(trail), str(delay), then],
        env=dict(os.environ),
        ready=lambda line: line == "ready",
        ready_timeout_s=timeout,
    )


def interrupt_self() -> None:
    os.kill(os.getpid(), signal.SIGINT)


def sup_trail(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


_RUN_SUPERVISOR = r"""
import os, sys
from pathlib import Path
from prismoid.local.supervisor import ChildSpec, Supervisor
home, trail, child, mode = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
specs = [
    ChildSpec(name=n, argv=[sys.executable, "-c", child, n, trail, "0", mode], env=dict(os.environ),
              ready=lambda line: line == "ready")
    for n in ("a", "b")
]
Supervisor(home, specs).up(on_ready=lambda: print("up", flush=True))
"""


@pytest.fixture(autouse=True)
def no_runtime_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """`up` builds Prismoid's Python: these tests fake `subprocess`, and a home's first build is minutes."""
    monkeypatch.setattr("prismoid.runner.local_cli.ensure_runtime", lambda home, **_: home)
    monkeypatch.setattr("prismoid.runner.local_cli.lanes_off", lambda env: None)


@pytest.fixture
def child_mode() -> str:
    return "wait"


@pytest.fixture
def running(home: Path, tmp_path: Path, child_mode: str) -> Iterator[tuple[subprocess.Popen[str], Path]]:
    trail = tmp_path / "trail"
    proc = subprocess.Popen(
        [sys.executable, "-c", _RUN_SUPERVISOR, str(home), str(trail), _CHILD, child_mode],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "up"
    yield proc, trail
    kill_stack(home, proc)
    proc.stdout.close()


def kill_stack(home: Path, proc: subprocess.Popen[str]) -> list[Process]:
    """What a test leaves running, whatever it was doing when it failed: the supervisor and its
    children's groups (each leads its own, and a deaf one ignores the SIGINT a clean stop sends).
    Killing the supervisor alone hands them to init. Returns the children it signalled."""
    try:
        children = json.loads((home / "run" / "prismoid.state").read_text())["children"]
    except FileNotFoundError:  # the stack stopped by itself
        children = {}
    if proc.poll() is None:
        proc.kill()
        proc.wait()
    stack = [Process(pid, born) for pid, born in children.values()]
    for child in stack:
        child.signal(signal.SIGKILL, group=True)
    return stack


class _FakeCluster:
    made: list[_FakeCluster]  # each test sets its own list (no state leaks between tests)
    stop_error: str | None = None

    def __init__(self, home: Path) -> None:
        self.home = home
        self.stopped = False
        type(self).made.append(self)

    @staticmethod
    def available() -> bool:
        return True

    def ensure(self, password: str) -> None:
        pass

    def start(self) -> Any:
        from prismoid.local.postgres import PgUrls

        return PgUrls(async_url="postgresql+asyncpg://x@/prismoid", sync_url="postgresql://x@/prismoid")

    def stop(self) -> None:
        self.stopped = True
        if self.stop_error:
            raise RuntimeError(self.stop_error)


def fake_cluster(monkeypatch: pytest.MonkeyPatch, stop_error: str | None = None) -> type[_FakeCluster]:
    fake = type("Cluster", (_FakeCluster,), {"made": [], "stop_error": stop_error})
    from prismoid.runner import local_cli

    monkeypatch.setattr(local_cli, "Cluster", fake)
    return fake


def fake_prep(monkeypatch: pytest.MonkeyPatch, home: Path, tmp_path: Path, *, keyed: bool = True) -> None:
    """Everything `up` does before its children, faked: no Postgres, no migrations, no first run."""
    from prismoid.runner import local_cli

    web = tmp_path / "web"
    web.mkdir(exist_ok=True)
    (web / "index.html").write_text("<!doctype html>")
    monkeypatch.setattr(settings.local, "home", home)
    monkeypatch.setattr(settings.local, "web_dir", web)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(local_cli, "_adopt", lambda env: None)
    monkeypatch.setattr(local_cli, "_migrate", lambda: None)
    monkeypatch.setattr(local_cli, "chrome_missing", lambda path: None)  # a box with no Chrome says so first
    monkeypatch.setattr(local_cli, "_port_taken", lambda port: False)
    monkeypatch.setattr(local_cli, "_ensure_owner", lambda json_out, web_url, owner: False)
    findings = [] if keyed else [{"check": "key-missing", "ref": "anthropic:claude-x", "detail": ""}]
    monkeypatch.setattr(local_cli, "_catalog_health", lambda: (["anthropic:claude-x"], findings))
