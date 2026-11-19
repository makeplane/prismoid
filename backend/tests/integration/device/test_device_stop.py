# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A helper that stops stops what it runs first (ADR-078 decision 5).

Real processes: a real `prismoid device helper` on a private home (uvloop, as in production), real
fenced command trees, real containers, real signals, a real supervisor. What is proved is what a
person would see in `ps` and in their folder, and what the next helper says:

- however the helper is asked to stop, no process of the tree and no container outlives it;
- the command it cut stays owed: no `finish` in the journal, reported as cut off, never as done;
- what the cut command planted is walked out of the folder by the stop itself;
- a helper killed outright can stop nothing, so the next one does.

This file is the tree: every way the helper is stopped, and what the stop plants and leaves. The rig
is `tests/support/rigs/device_stop_rig.py`."""

from __future__ import annotations

import contextlib
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from prismoid.device.frames import (
    FRAME_FILE,
    STOP_GRACE_S,
    STOP_KILL_S,
)
from prismoid.device.plants import QUARANTINE
from prismoid.local.supervisor import InstanceLock, Supervisor
from tests.support.markers import darwin_only
from tests.support.rigs.device_stop_rig import (
    COMMAND_HEAD,
    PLANT,
    RUN_UP,
    TREE_FILES,
    Helper,
    Running,
    Spawned,
    alive,
    assert_gone_at_exit,
    assert_owed,
    grant,
    holds_text,
    until,
)
from tests.support.rigs.device_stop_rig import folder as folder
from tests.support.rigs.device_stop_rig import home as home
from tests.support.rigs.device_stop_rig import spawned as spawned


@darwin_only
@pytest.mark.parametrize(
    "sig", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP], ids=["ctrl-c", "sigterm", "terminal-closed"]
)
def test_a_stopping_helper_takes_the_whole_tree_with_it(
    home: Path, spawned: Spawned, sig: signal.Signals
) -> None:
    helper = Helper(home, spawned)
    running = Running(helper, spawned)
    assert running.survivors() == sorted(running.pids) and len(running.pids) >= len(TREE_FILES)

    helper.stop(sig)

    assert_gone_at_exit(running)
    assert_owed(running, home, spawned)


@darwin_only
def test_prismoid_down_stops_the_commands_of_a_stack(home: Path, spawned: Spawned) -> None:
    """The way `prismoid up` and `prismoid down` stop it: the supervisor's SIGINT, inside its budget."""
    helper = Helper(home, spawned, _stack(home))
    running = Running(helper, spawned)
    started = time.monotonic()
    Supervisor(home, []).down(timeout_s=30)
    assert time.monotonic() - started < Supervisor.stop_timeout_s
    assert running.survivors() == []
    assert not InstanceLock(home).taken()
    assert_owed(running, home, spawned)


def _stack(home: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", RUN_UP, str(home)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=home,
    )


@darwin_only
def test_a_second_ctrl_c_leaves_no_tree_behind(home: Path, spawned: Spawned) -> None:
    """A double tap on Ctrl-C, through the real supervisor and the real helper spec: the helper keeps
    the time its stop needs, because its commands are in sessions a kill of it never reaches."""
    stack = _stack(home)
    helper = Helper(home, spawned, stack)
    running = Running(helper, spawned)

    stack.send_signal(signal.SIGINT)
    time.sleep(0.05)
    stack.send_signal(signal.SIGINT)
    stack.wait(timeout=Supervisor.stop_timeout_s + 5)

    assert running.survivors() == []
    assert not InstanceLock(home).taken()
    assert_owed(running, home, spawned)


@darwin_only
def test_a_helper_killed_outright_leaves_it_to_the_next(home: Path, spawned: Spawned) -> None:
    """Killed before any look saw the `setsid` child: the next life finds it through its parent."""
    helper = Helper(home, spawned)
    running = Running(helper, spawned)

    helper.proc.kill()
    helper.proc.wait(timeout=10)

    assert running.survivors() == sorted(running.pids)  # nobody could stop it
    # The next life's start kills it, and the cut stays reported.
    assert_owed(
        running, home, spawned, lambda: until(lambda: not running.survivors(), 5, "the tree lived on")
    )


@darwin_only
def test_a_torn_frame_file_strands_no_tree(home: Path, spawned: Spawned) -> None:
    """Not a frame any more, but what it ran is still found by the groups beside it."""
    helper = Helper(home, spawned)
    running = Running(helper, spawned)
    helper.proc.kill()
    helper.proc.wait(timeout=10)
    (running.base / FRAME_FILE).write_text('{"ref": "dev')

    next_life = Helper(home, spawned)

    until(lambda: not running.survivors(), 5, "the tree lived on")
    next_life.stop()


@darwin_only
def test_a_journal_torn_by_a_crash_still_reports_the_next_cut(home: Path, spawned: Spawned) -> None:
    helper = Helper(home, spawned)
    done = helper.frame()
    assert helper.run(done["ref"], "true").json()["exit_code"] == 0
    journal = home / "tasks" / done["ref"].removeprefix("device-") / "journal.jsonl"
    with journal.open("a") as f:
        f.write('{"event": "start", "exec_id": "to')

    body = {"exec_id": "after-the-tear", "command": "sleep 300", "timeout": 120}

    def post() -> None:
        with contextlib.suppress(httpx.HTTPError):
            helper.door.post(f"/frames/{done['ref']}/exec", json=body)

    threading.Thread(target=post, daemon=True).start()
    until(lambda: "after-the-tear" in journal.read_text(), 10, "it never started")
    helper.stop()

    next_life = Helper(home, spawned)
    said = next_life.door.get(f"/frames/{done['ref']}/interrupted").json()
    assert (said["exec_id"], said["still_running"]) == ("after-the-tear", False)
    next_life.stop()


@darwin_only
def test_what_a_cut_command_planted_goes_with_the_stop(home: Path, spawned: Spawned, folder: Path) -> None:
    """The normal end's walk, at the stop: not left in the person's folder while Prismoid is down, where
    a planted repository's hooks run unfenced. Even a plant made on the way out: a trap on SIGTERM."""
    helper = Helper(home, spawned)
    command = (
        f"{COMMAND_HEAD}\n{PLANT.format(d='early')} trap '{PLANT.format(d='late')} exit' TERM\n"
        "echo $$ > root.pid; sleep 300 & wait"
    )
    running = Running(helper, spawned, command, ("root",), grants=grant(folder))

    helper.stop()

    planted = [p for p in folder.rglob("*") if p.name == "HEAD" or p.is_symlink()]
    assert planted == []
    assert list((home / QUARANTINE).rglob("HEAD")), "moved, never deleted"
    assert_gone_at_exit(running)

    next_life = Helper(home, spawned)
    told = next_life.run(running.ref, "true").json()["stderr"]
    assert "out-early" in told, "the agent hears what the stop moved, with its next command"
    next_life.stop()


@darwin_only
def test_a_child_a_leftovers_sigterm_handler_starts_is_stopped_too(home: Path, spawned: Spawned) -> None:
    """The stop's own SIGTERM ends every process it had seen in the group, inside one look, and leaves
    a child they had just started: the group stays the frame's for the stop."""
    helper = Helper(home, spawned)
    frame = helper.frame()
    work = Path(frame["paths"]["work"])
    left = (
        "(trap 'sleep 300 >/dev/null 2>&1 & echo $! > respawn.pid; exit 0' TERM; "
        "while :; do sleep 0.1; done) >/dev/null 2>&1 &"
    )
    assert helper.run(frame["ref"], left).json()["exit_code"] == 0

    helper.proc.send_signal(signal.SIGTERM)
    respawn = work / "respawn.pid"
    pid = spawned.note(
        int(until(lambda: holds_text(respawn) and respawn.read_text().strip(), 5, "no respawn"))
    )
    helper.proc.wait(timeout=STOP_GRACE_S + STOP_KILL_S + 5)

    assert not alive(pid)
