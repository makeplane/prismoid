# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# backend/prismoid/ai/frames/image_browser/sidecar/ctl.py
"""The client the driver execs as the `browser` user. Command in `SIDECAR_CMD` (env, readable
only by this uid), response on stdout, exit 0 always: an error is a result, not a crash.

It runs once per browser call, so it imports only what the call needs: the command and the reply
pass through as bytes, and `json` and `subprocess` load only on the paths that read a verb or
start the daemon. A start waits on the daemon's own signal (its stdout closing once the socket
listens, or at its death), never on a polling sleep."""

from __future__ import annotations

import errno
import os
import socket
import sys

# Verbs that act on a running daemon only. With none running there is nothing to move or stop, and
# neither may start Chromium: a step boundary's `record` (the next spawn reads the step's folder
# from its own environment), and a `shutdown` that would otherwise launch Chromium over the profile
# a sleep is archiving.
NOTHING_TO_DO_WHEN_STOPPED = frozenset({"record", "shutdown"})
# The most a cold start (the daemon, its Chromium and the CDP attach) is waited for. A start under
# CPU contention ran past 6 s; the wait still ends at the daemon's ready signal or at its death.
START_S = 25.0


def _say(reply: dict[str, object]) -> int:
    """Print one result line and exit 0."""
    import json

    print(json.dumps(reply))
    return 0


def _verb(raw: str) -> str | None:
    """The command's verb; None for one the daemon will refuse (not JSON, not an object, no string verb)."""
    import json

    try:
        verb = json.loads(raw).get("verb")
    except Exception:
        return None
    return verb if isinstance(verb, str) else None


def _connect(sock: str) -> socket.socket | None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(sock)
        return s
    except OSError:
        s.close()
        return None


def _start(sock: str) -> socket.socket | None:
    """Start the daemon and connect once it says its socket listens (or it died, or `START_S` passed).

    One start at a time: a call that finds no socket while another is starting the daemon waits on
    the start lock (a flock beside the socket), then connects to that daemon instead of spawning a
    second one whose Chromium would die on the first one's profile lock. The holder waits at most
    `START_S`, and the kernel releases the lock if it dies."""
    import fcntl
    import select
    import subprocess

    with open(sock + ".start", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        conn = _connect(sock)
        if conn is not None:
            return conn
        env = {k: v for k, v in os.environ.items() if k != "SIDECAR_CMD"}
        # No `setsid` binary here: start_new_session=True already detaches the daemon into its own
        # session, and `setsid` doesn't exist on macOS dev boxes anyway.
        daemon = subprocess.Popen(
            [sys.executable, "-m", "sidecar.daemon"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        assert daemon.stdout is not None
        with daemon.stdout:
            select.select([daemon.stdout], [], [], START_S)
        return _connect(sock)


def main() -> int:
    sock = os.environ.get("SIDECAR_SOCK", "/run/browser/sidecar.sock")
    raw = os.environ.get("SIDECAR_CMD", "")
    if not raw:
        return _say({"ok": False, "error": "no SIDECAR_CMD"})
    # `record` moves a running daemon to the step folder this exec names - the same
    # `SIDECAR_RECORDING` a spawn reads, so a move and a spawn always agree on it.
    if "record" in raw and _verb(raw) == "record":
        raw = _record()
    try:
        conn = _connect(sock)
        if conn is None:
            if _verb(raw) in NOTHING_TO_DO_WHEN_STOPPED:
                return _say({"ok": True})
            conn = _start(sock)
            if conn is None:
                return _say({"ok": False, "error": "sidecar did not start"})
        with conn:
            # A wedged daemon (a hung Chromium, a dead pipe it hasn't noticed yet) must not leak
            # this thread forever on the driver side - Docker's `exec_run` call blocks on ctl.py's
            # own exit, so a ctl.py that never returns is a dev-stack thread leak per timed-out
            # call. An error is still a result (module docstring), never a crash: exit 0 either way.
            conn.settimeout(float(os.environ.get("SIDECAR_CTL_TIMEOUT_S", "60")))
            conn.sendall(raw.encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(1 << 16)
                if not chunk:
                    break
                buf += chunk
    except TimeoutError:
        return _say({"ok": False, "error": "sidecar timed out"})
    except OSError as e:
        name = errno.errorcode.get(e.errno, str(e.errno)) if e.errno is not None else type(e).__name__
        return _say({"ok": False, "error": f"sidecar unreachable: {name}"})
    if not buf:
        return _say({"ok": False, "error": "empty reply"})
    sys.stdout.write(buf.decode())
    return 0


def _record() -> str:
    """The `record` command naming this exec's folder."""
    import json

    return json.dumps({"verb": "record", "dir": os.environ.get("SIDECAR_RECORDING", "")})


if __name__ == "__main__":
    raise SystemExit(main())
