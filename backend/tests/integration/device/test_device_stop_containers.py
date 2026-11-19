# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The containers of a stop (ADR-078 decision 5): a stop stops the frame's
containers and nothing else, the next life stops what a killed helper left, and a hung docker costs its
wait and loses nothing. The rig is `tests/support/rigs/device_stop_rig.py`."""

from __future__ import annotations

import shutil
import signal
import socket
import threading
import time
import uuid
from pathlib import Path

import pytest

from prismoid.device import docker_journal
from prismoid.device.docker_proxy import STOP_DOCKER_S
from prismoid.device.frames import (
    STOP_GRACE_S,
    STOP_KILL_S,
    SWEEP_WAIT_S,
)
from prismoid.local.supervisor import Supervisor
from tests.support.markers import cannot_run, darwin_only
from tests.support.rigs.device_stop_rig import (
    CHROME_STOP_S,
    IMAGE,
    Helper,
    Running,
    Spawned,
    assert_gone_at_exit,
    grant,
    stop_docker,
    until,
)
from tests.support.rigs.device_stop_rig import folder as folder
from tests.support.rigs.device_stop_rig import home as home
from tests.support.rigs.device_stop_rig import spawned as spawned


def test_the_stop_fits_inside_the_supervisors_patience() -> None:
    """Past `stop_timeout_s` the supervisor kills the helper outright. The commands, then Docker
    beside Chrome, then the walk, in the worst case of each."""
    patience = STOP_GRACE_S + STOP_KILL_S + max(STOP_DOCKER_S, CHROME_STOP_S) + SWEEP_WAIT_S
    assert patience < Supervisor.stop_timeout_s


@pytest.fixture
def daemon() -> None:
    if shutil.which("docker") is None or stop_docker("info", check=False).returncode != 0:
        cannot_run("Docker is not running")


def _state(ident: str) -> str:
    return stop_docker("inspect", "-f", "{{.State.Status}}", ident).stdout.strip()


def _ticks(folder: Path) -> int:
    tick = folder / "tick"
    return tick.read_text().count("\n") if tick.exists() else 0


def _ticking_container(helper: Helper, spawned: Spawned, ref: str, folder: Path) -> str:
    """A container the frame's command starts through its proxy, writing the grant every 100 ms."""
    script = "while :; do date >> /w/tick; sleep 0.1; done"
    run = helper.run(ref, f"docker run -d -v \"$PWD\":/w {IMAGE} sh -c '{script}'").json()
    assert run["exit_code"] == 0, run["stderr"]
    ident = run["stdout"].strip()
    spawned.containers.append(ident)
    until(lambda: _ticks(folder), 20, "it never wrote")
    return ident


@darwin_only
def test_a_stop_stops_the_frames_containers_and_nothing_else(
    home: Path, spawned: Spawned, folder: Path, daemon: None
) -> None:
    """Stopped, not removed, and no stranger touched: not one wearing the frame's label that the
    proxy never made (a label is no proof), and not an ordinary neighbour."""
    helper = Helper(home, spawned)
    frame = helper.frame(grants=grant(folder), docker=True)
    ref = frame["ref"]
    mine = _ticking_container(helper, spawned, ref, folder)
    volume = f"prismoid-stop-{uuid.uuid4().hex[:8]}"
    marked = stop_docker(
        "run", "-d", "--label", f"prismoid.frame={ref}", "-v", f"{volume}:/d", IMAGE, "sleep", "300"
    )
    neighbour = stop_docker("run", "-d", IMAGE, "sleep", "300")
    forged, other = marked.stdout.strip(), neighbour.stdout.strip()
    spawned.containers += [forged, other]

    try:
        helper.stop(signal.SIGINT)

        assert _state(mine) == "exited", "a container has no fence: none outlives its helper"
        wrote = _ticks(folder)
        time.sleep(0.5)
        assert _ticks(folder) == wrote
        assert (_state(forged), _state(other)) == ("running", "running")
    finally:
        stop_docker("rm", "-f", forged, other, check=False)
        stop_docker("volume", "rm", volume, check=False)


@darwin_only
def test_a_paused_container_is_stopped_too(home: Path, spawned: Spawned, folder: Path, daemon: None) -> None:
    """A frozen container still holds its bind and its ports, and thaws whenever anyone says so."""
    helper = Helper(home, spawned)
    ref = helper.frame(grants=grant(folder), docker=True)["ref"]
    mine = _ticking_container(helper, spawned, ref, folder)
    stop_docker("pause", mine)
    assert _state(mine) == "paused"

    helper.stop(signal.SIGINT)

    assert _state(mine) == "exited"


@darwin_only
def test_the_next_life_stops_what_a_killed_helper_left(
    home: Path, spawned: Spawned, folder: Path, daemon: None
) -> None:
    helper = Helper(home, spawned)
    ref = helper.frame(grants=grant(folder), docker=True)["ref"]
    mine = _ticking_container(helper, spawned, ref, folder)

    helper.proc.kill()
    helper.proc.wait(timeout=10)
    assert _state(mine) == "running", "nobody could stop it"

    next_life = Helper(home, spawned)

    assert _state(mine) == "exited", "stopped before its door opened"
    next_life.stop()


class _HungDocker:
    """A daemon that takes every connection and never answers one."""

    def __init__(self, path: Path) -> None:
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(16)
        self.held: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        with self.server:
            while True:
                try:
                    self.held.append(self.server.accept()[0])
                except OSError:
                    return


@darwin_only
def test_a_hung_docker_costs_its_wait_and_loses_nothing(home: Path, spawned: Spawned) -> None:
    """The commands stop first, the walk and the exit still come, and all inside the budget."""
    if shutil.which("docker") is None:
        cannot_run("no Docker CLI")  # it names the socket; the daemon here is this test's
    hung = home / "hung.sock"
    _HungDocker(hung)
    env = {"DOCKER_HOST": f"unix://{hung}"}
    helper = Helper(home, spawned, env=env)
    running = Running(helper, spawned)
    docker_journal.Journal(home / "run").record(running.ref, "containers", "c" * 64)

    started = time.monotonic()
    helper.proc.send_signal(signal.SIGINT)
    until(lambda: not running.survivors(), STOP_GRACE_S + STOP_KILL_S + STOP_DOCKER_S, "the tree lived on")
    assert time.monotonic() - started < STOP_GRACE_S + STOP_DOCKER_S - 1, "the commands never wait on Docker"
    helper.proc.wait(timeout=Supervisor.stop_timeout_s)
    assert time.monotonic() - started < Supervisor.stop_timeout_s

    assert_gone_at_exit(running)
    journal = (running.base / "journal.jsonl").read_text()
    assert '"finish"' not in journal, "the cut is still owed"
    # A dead daemon at the next start costs the same, and opens the door all the same.
    next_life = Helper(home, spawned, env=env)
    assert next_life.door.get(f"/frames/{running.ref}/interrupted").json()["exec_id"] == running.exec_id
    next_life.stop()
