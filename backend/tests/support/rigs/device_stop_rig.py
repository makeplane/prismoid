# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The rig for the stopping-helper tests (ADR-078 decision 5), shared by the
`test_device_stop*` files: a real `prismoid device helper` on a private home, its fenced command trees, and
what a person would see in `ps` and in their folder. A plain module, not a test file."""

from __future__ import annotations

import asyncio
import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from prismoid.device import procs
from prismoid.device.docker_proxy import STOP_DOCKER_S
from prismoid.device.frames import (
    STOP_GRACE_S,
    STOP_KILL_S,
    SWEEP_WAIT_S,
    FrameRegistry,
    force_rmtree,
)
from prismoid.device.helper import KEY_HEADER
from prismoid.device.seatbelt import alias_root, cache_root

USER_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)


IMAGE = "alpine:3.20"


CHROME_STOP_S = 6.0  # `Chrome.close`: a browser that will not go is reaped in about this


COMMAND_HEAD = "# the long test command"


# What `npm run test:slow` is to the helper, and what would escape a stop that looks at less: a
# plain child, one that ignores SIGTERM (with a child of its own) and one in a session of its own,
# outside the command's process group. Each writes its pid and then just lives.
TREE = f"""{COMMAND_HEAD}
echo $$ > root.pid
sleep 300 & echo $! > plain.pid
bash -c 'trap "" TERM; echo $$ > stubborn.pid; sleep 300 & echo $! > stubborn-child.pid; wait' &
perl -MPOSIX -e 'POSIX::setsid(); open(F, ">", "escaped.tmp"); print F $$; close(F);
rename("escaped.tmp", "escaped.pid"); sleep 300' &
wait
"""


TREE_FILES = ("root", "plain", "stubborn", "stubborn-child", "escaped")


# A stack as `prismoid up` makes it, with the real helper spec, `keeps_its_stop` and all.
RUN_UP = """
import dataclasses, os, sys
from pathlib import Path
from prismoid.local.postgres import PgUrls
from prismoid.local.supervisor import Supervisor
from prismoid.runner.local_cli import child_env, child_specs
home = Path(sys.argv[1])
specs = child_specs(home, child_env(home, PgUrls("a", "s"), home, 8100), 8100)
helper = next(s for s in specs if s.name == "helper")
env = {**helper.env, "PYTHONPATH": os.pathsep.join(sys.path)}
Supervisor(home, [dataclasses.replace(helper, env=env, ready=lambda _: True)]).up()
"""


# A link out of the grant and the shape of a bare repository, whose hooks run unfenced.
PLANT = "ln -s /etc/hosts out-{d}; mkdir -p sub/{d}/objects sub/{d}/refs; echo ref: refs/heads/main > sub/{d}/HEAD;"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def until(ready: Callable[[], Any], within: float, what: str) -> Any:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if found := ready():
            return found
        time.sleep(0.02)
    raise AssertionError(f"{what} within {within} s")


def holds_text(path: Path) -> bool:
    return path.exists() and bool(path.read_text().strip())


async def written(path: Path) -> None:
    """The command has written its pid: it runs."""
    for _ in range(1000):
        if holds_text(path):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{path.name} never came")


def tree(root: int) -> set[int]:
    """`root` and everything under it now, from `ps`, not from anything the helper keeps."""
    lines = subprocess.run(["/bin/ps", "-axo", "pid=,ppid="], capture_output=True, text=True, check=True)
    children: dict[int, list[int]] = {}
    for line in lines.stdout.splitlines():
        pid, parent = map(int, line.split())
        children.setdefault(parent, []).append(pid)
    found, pending = {root}, [root]
    while pending:
        for pid in children.get(pending.pop(), ()):
            found.add(pid)
            pending.append(pid)
    return found


class Spawned:
    """Everything a test started, by pid and start time, for a test that fails half way: the only
    processes the teardown ever signals."""

    def __init__(self) -> None:
        self.helpers: list[subprocess.Popen[bytes]] = []
        self.pids: dict[int, int | None] = {}
        self.containers: list[str] = []
        self.folders: list[Path] = []
        self.frames: list[str] = []  # a torn frame file hides a frame from the registry: its cache by ref

    def note(self, pid: int) -> int:
        self.pids.setdefault(pid, procs.started(pid))
        return pid

    def tear_down(self) -> None:
        for proc in self.helpers:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        for pid, at in self.pids.items():
            if at is not None and procs.started(pid) == at:
                os.kill(pid, signal.SIGKILL)
        for ident in self.containers:
            stop_docker("rm", "-f", ident, check=False)
        for folder in self.folders:
            shutil.rmtree(folder, ignore_errors=True)
        for ref in self.frames:
            force_rmtree(Path(cache_root(ref)))
            force_rmtree(Path(alias_root(ref)))


class Helper:
    """One life of the real helper on `home` (or the one a stack started), at its own socket."""

    def __init__(
        self,
        home: Path,
        spawned: Spawned,
        proc: subprocess.Popen[bytes] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.home, self.spawned = home, spawned
        self.proc = proc or subprocess.Popen(
            [sys.executable, "-m", "prismoid.runner.main", "device", "helper", "--home", str(home)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=home,
            start_new_session=True,
            env={**os.environ, **(env or {})},
        )
        spawned.helpers.append(self.proc)
        self.door: httpx.Client = until(self._connect, 30, "the helper did not answer")

    def _connect(self) -> httpx.Client | None:
        key_file, sock = self.home / "helper.key", self.home / "run" / "helper.sock"
        assert self.proc.poll() is None, f"the helper exited: {self.proc.returncode}"
        if not key_file.exists() or not sock.exists():
            return None
        door = httpx.Client(
            transport=httpx.HTTPTransport(uds=str(sock)),
            base_url="http://helper",
            headers={KEY_HEADER: key_file.read_text().strip()},
            timeout=None,
        )
        try:
            return door if door.get("/healthz").status_code == 200 else None
        except httpx.TransportError:
            return None

    def frame(self, **spec: Any) -> dict[str, Any]:
        made = self.door.post("/frames", json={"task_id": uuid.uuid4().hex[:8], "kind": "machine", **spec})
        assert made.status_code == 200, made.text
        self.spawned.frames.append(made.json()["ref"])
        return made.json()

    def run(self, ref: str, command: str) -> httpx.Response:
        body = {"exec_id": uuid.uuid4().hex, "command": command, "timeout": 120}
        return self.door.post(f"/frames/{ref}/exec", json=body)

    def stop(self, sig: signal.Signals = signal.SIGTERM) -> None:
        self.proc.send_signal(sig)
        self.proc.wait(timeout=STOP_GRACE_S + STOP_KILL_S + STOP_DOCKER_S + SWEEP_WAIT_S + 5)


class Running:
    """A command tree running under a helper, its exec in flight on a thread of its own."""

    def __init__(
        self,
        helper: Helper,
        spawned: Spawned,
        command: str = TREE,
        names: tuple[str, ...] = TREE_FILES,
        **spec: Any,
    ) -> None:
        self.exec_id = uuid.uuid4().hex
        frame = helper.frame(**spec)
        self.ref, self.work = frame["ref"], Path(frame["paths"]["work"])
        self.base = helper.home / "tasks" / self.ref.removeprefix("device-")
        self.answer: httpx.Response | Exception | None = None
        body = {"exec_id": self.exec_id, "command": command, "timeout": 120}

        def call() -> None:
            try:
                self.answer = helper.door.post(f"/frames/{self.ref}/exec", json=body)
            except httpx.HTTPError as e:
                self.answer = e

        self.thread = threading.Thread(target=call, daemon=True)
        self.thread.start()
        self.pids: set[int] = set()
        for name in names:  # noted as each appears: a failure here still leaves nothing behind
            self.pids |= {spawned.note(p) for p in tree(self._pid(name))}
        for pid in list(self.pids):
            self.pids |= {spawned.note(p) for p in tree(pid)}

    def _pid(self, name: str) -> int:
        path = self.work / f"{name}.pid"
        return int(until(lambda: holds_text(path) and path.read_text().strip(), 20, f"no {name}.pid"))

    def survivors(self) -> list[int]:
        return sorted(pid for pid in self.pids if alive(pid))


@pytest.fixture
def home() -> Iterator[Path]:
    """A short path: a Unix socket's fits in 104 bytes, and pytest's tmp_path is long already."""
    path = Path(tempfile.mkdtemp(prefix="prismoid-stop-", dir="/tmp")).resolve()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def spawned(home: Path) -> Iterator[Spawned]:
    """Whatever a failing test left, by the pids it recorded; then its frames' caches."""
    made = Spawned()
    try:
        yield made
    finally:
        made.tear_down()
        registry = FrameRegistry(home)
        for ref in list(registry.frames):
            registry.destroy(ref)


@pytest.fixture
def folder(spawned: Spawned) -> Path:
    """A folder in the user's real home: a grant must be there."""
    path = Path(tempfile.mkdtemp(prefix="prismoid-stop-", dir=USER_HOME)).resolve()
    spawned.folders.append(path)
    return path


def grant(folder: Path) -> list[dict[str, str]]:
    return [{"path": str(folder), "mode": "rw", "name": "/work"}]


def assert_owed(
    running: Running, home: Path, spawned: Spawned, while_up: Callable[[], Any] = lambda: None
) -> None:
    """The next helper life says what a resumed agent is told: the command, cut off, not finished.
    `while_up` is asked while it runs: its own orderly stop, at the end here, would hide the rest."""
    journal = (running.base / "journal.jsonl").read_text().splitlines()
    assert [e["event"] for e in map(json.loads, journal) if e["exec_id"] == running.exec_id] == ["start"]
    next_life = Helper(home, spawned)
    said = next_life.door.get(f"/frames/{running.ref}/interrupted")
    assert said.status_code == 200
    cut = said.json()
    assert cut["exec_id"] == running.exec_id
    assert cut["command_head"].startswith(COMMAND_HEAD)
    assert (cut["still_running"], cut["exit_code"]) == (False, None)
    while_up()
    next_life.stop()


def assert_gone_at_exit(running: Running) -> None:
    assert running.survivors() == []  # before it exits, not some time after
    running.thread.join(timeout=5)
    # A live worker hears the helper went away (`FrameUnreachable`), never a command's result.
    answer = running.answer
    assert isinstance(answer, httpx.TransportError) or (
        isinstance(answer, httpx.Response) and answer.status_code == 503
    )


def stop_docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """The user's own Docker CLI, unfenced: for setup, checks and our own teardown only."""
    env = {k: v for k, v in os.environ.items() if k not in ("DOCKER_HOST", "DOCKER_CONFIG")}
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=120, check=check, env=env
    )
