# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# backend/tests/unit/ai/frames/image_browser/test_sidecar_cold_start.py
"""A cold start of the sidecar (the daemon and its Chromium) can take longer than a warm call:
`ctl.py` must wait for it, and a second call arriving mid-start must wait for the same daemon
instead of spawning a second one that trips over the first's profile lock."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from tests.support.paths import BACKEND

CTL = BACKEND / "prismoid" / "ai" / "frames" / "image_browser" / "sidecar" / "ctl.py"

# A stand-in for `sidecar.daemon`: notes its spawn, takes the profile lock the way Chromium does
# (a second holder dies), takes DELAY seconds to "launch Chromium", then listens and closes stdout.
FAKE_DAEMON = r"""
import fcntl, os, socket, sys, time
root = os.environ["FAKE_ROOT"]
with open(os.path.join(root, "spawns"), "a") as f:
    f.write(f"{os.getpid()}\n")
lock = open(os.path.join(root, "profile.lock"), "w")
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(21)  # Chromium: "The profile appears to be in use by another process"
time.sleep(float(os.environ["FAKE_DELAY_S"]))
sock = os.environ["SIDECAR_SOCK"]
try:
    os.unlink(sock)
except FileNotFoundError:
    pass
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(sock)
srv.listen(8)
devnull = os.open(os.devnull, os.O_WRONLY)
os.dup2(devnull, 1)
end = time.time() + 30
srv.settimeout(1)
while time.time() < end:
    try:
        conn, _ = srv.accept()
    except OSError:
        continue
    with conn:
        conn.recv(65536)
        conn.sendall(b'{"ok": true, "pid": %d}\n' % os.getpid())
"""


def _fake_sidecar(delay_s: float) -> tuple[Path, dict[str, str]]:
    root = Path(tempfile.mkdtemp(dir="/tmp", prefix="sc-"))  # AF_UNIX path length cap on macOS
    pkg = root / "sidecar"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    shutil.copy(CTL, pkg / "ctl.py")
    (pkg / "daemon.py").write_text(FAKE_DAEMON)
    env = dict(os.environ)
    env.update(
        PYTHONPATH=str(root),
        SIDECAR_SOCK=str(root / "s.sock"),
        SIDECAR_CMD=json.dumps({"verb": "snapshot"}),
        FAKE_ROOT=str(root),
        FAKE_DELAY_S=str(delay_s),
    )
    return root, env


def _ctl(env: dict[str, str]) -> dict[str, object]:
    out = subprocess.run(
        [sys.executable, "-m", "sidecar.ctl"], env=env, capture_output=True, text=True, timeout=60
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def _spawns(root: Path) -> list[str]:
    path = root / "spawns"
    return path.read_text().split() if path.exists() else []


def _kill_daemons(root: Path) -> None:
    for pid in _spawns(root):
        with contextlib.suppress(OSError):
            os.kill(int(pid), 9)
    shutil.rmtree(root, ignore_errors=True)


def test_a_cold_start_slower_than_six_seconds_still_answers_the_first_call() -> None:
    root, env = _fake_sidecar(delay_s=8.0)
    try:
        reply = _ctl(env)
    finally:
        _kill_daemons(root)
    assert reply.get("ok") is True, reply


def test_a_second_call_during_a_cold_start_waits_for_the_same_daemon() -> None:
    root, env = _fake_sidecar(delay_s=8.0)
    replies: list[dict[str, object]] = []
    try:
        first = threading.Thread(target=lambda: replies.append(_ctl(env)))
        first.start()
        threading.Event().wait(6.5)  # the retry lands after ctl's 6 s give-up, before the daemon listens
        second = _ctl(env)
        first.join(timeout=60)
        spawns = _spawns(root)
    finally:
        _kill_daemons(root)
    assert second.get("ok") is True, second
    assert len(spawns) == 1, f"{len(spawns)} daemons spawned for one cold start"
