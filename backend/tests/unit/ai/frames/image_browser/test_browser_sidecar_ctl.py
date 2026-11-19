# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# backend/tests/unit/ai/frames/image_browser/test_browser_sidecar_ctl.py
"""`ctl.py`'s socket timeout: a wedged daemon (a hung Chromium, a pipe it hasn't noticed died)
must not leak the driver-side `exec_run` thread forever - a timed-out reply is still a result
(exit 0), never a hang."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from tests.support.paths import BACKEND

SIDECAR = BACKEND / "prismoid" / "ai" / "frames" / "image_browser"


def test_ctl_times_out_and_exits_0_when_the_daemon_never_replies() -> None:
    # AF_UNIX paths are capped at ~104 bytes on macOS (sockaddr_un); pytest's tmp_path lives
    # under a long per-test directory that overflows that limit there (the daemon tests hit the
    # same wall), so the socket gets its own short-named directory under /tmp instead.
    sock_dir = tempfile.mkdtemp(dir="/tmp", prefix="sc-")
    sock_path = str(Path(sock_dir) / "s.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    conns: list[socket.socket] = []

    def accept_and_hang() -> None:
        conn, _ = server.accept()
        conns.append(conn)  # keep a reference alive so nothing closes it out from under recv()
        with contextlib.suppress(OSError):
            while conn.recv(4096):  # drain whatever arrives, reply to none of it
                pass

    thread = threading.Thread(target=accept_and_hang, daemon=True)
    thread.start()

    env = dict(os.environ)
    env["SIDECAR_SOCK"] = sock_path
    env["SIDECAR_CMD"] = json.dumps({"verb": "ping"})
    env["SIDECAR_CTL_TIMEOUT_S"] = "0.3"
    env["PYTHONPATH"] = str(SIDECAR)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "sidecar.ctl"], env=env, capture_output=True, text=True, timeout=15
        )
    finally:
        server.close()
        thread.join(timeout=2)
        shutil.rmtree(sock_dir, ignore_errors=True)

    assert result.returncode == 0
    assert json.loads(result.stdout.strip()) == {"ok": False, "error": "sidecar timed out"}


def _ctl(sock_path: str, cmd: dict[str, object], recording: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(
        SIDECAR_SOCK=sock_path,
        SIDECAR_CMD=json.dumps(cmd),
        SIDECAR_RECORDING=recording,
        PYTHONPATH=str(SIDECAR),
    )
    return subprocess.run(
        [sys.executable, "-m", "sidecar.ctl"], env=env, capture_output=True, text=True, timeout=15
    )


def test_record_names_the_folder_this_exec_was_given() -> None:
    """`record` moves a running daemon to the folder its exec names - the same `SIDECAR_RECORDING`
    a spawn reads - so a spawn and a move can never disagree about where a step records."""
    sock_dir = tempfile.mkdtemp(dir="/tmp", prefix="sc-")
    sock_path = str(Path(sock_dir) / "s.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    received: list[bytes] = []

    def answer() -> None:
        conn, _ = server.accept()
        with conn:
            received.append(conn.recv(4096))
            conn.sendall(b'{"ok": true}\n')

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    try:
        result = _ctl(sock_path, {"verb": "record"}, "/artifacts/recording-bbbbbbbb-1")
    finally:
        server.close()
        thread.join(timeout=2)
        shutil.rmtree(sock_dir, ignore_errors=True)
    assert json.loads(result.stdout.strip()) == {"ok": True}
    assert json.loads(received[0]) == {"verb": "record", "dir": "/artifacts/recording-bbbbbbbb-1"}


@pytest.mark.parametrize("verb", ["record", "shutdown"])
def test_a_verb_with_nothing_to_do_when_no_daemon_runs_starts_none(verb: str) -> None:
    """No daemon is no folder to move (the next command's spawn reads this step's folder from its
    own environment) and no Chromium to stop: a step boundary never starts Chromium, and neither
    does a stop - one racing a sleep's archive would launch it over the profile being archived."""
    sock_dir = tempfile.mkdtemp(dir="/tmp", prefix="sc-")
    try:
        result = _ctl(str(Path(sock_dir) / "s.sock"), {"verb": verb}, "/artifacts/recording-x")
        assert not (Path(sock_dir) / "s.sock").exists()
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)
    assert result.returncode == 0
    assert json.loads(result.stdout.strip()) == {"ok": True}
