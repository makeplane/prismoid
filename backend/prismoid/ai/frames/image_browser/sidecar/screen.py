# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# backend/prismoid/ai/frames/image_browser/sidecar/screen.py
"""The screen's bridge: `python3 -m sidecar.screen`, exec'd as the `browser` user,
pumps bytes between its own stdin/stdout and the daemon's socket in screen mode. It opens no
listening port (the posture of `ctl.py`) and parses nothing.

It never starts the daemon: `ctl.py` starts it with the first step's environment
(the recording folder, the proxy), which holds for the daemon's life, and a viewer must not choose
that. With no daemon listening it writes one `bye{reason: "no_browser"}` and exits 0; the relay
backs off and tries again."""

from __future__ import annotations

import contextlib
import os
import socket
import sys
import threading

from .wire import encode

HANDSHAKE = b'{"verb": "screen"}\n'
CHUNK = 1 << 16


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _connect(path: str) -> socket.socket | None:
    """One try, never a spawn: a missing socket, or a stale one, is "no browser"."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.connect(path)
    except OSError:
        conn.close()
        return None
    return conn


def _up(conn: socket.socket, stdin: int) -> None:
    """stdin -> the daemon until stdin ends (the relay closed the exec); then a half-close, so the
    daemon ends this screen and the down pump in `main` sees EOF."""
    with contextlib.suppress(OSError):
        while chunk := os.read(stdin, CHUNK):
            conn.sendall(chunk)
    with contextlib.suppress(OSError):
        conn.shutdown(socket.SHUT_WR)


def main() -> int:
    stdin, stdout = sys.stdin.fileno(), sys.stdout.fileno()
    conn = _connect(os.environ.get("SIDECAR_SOCK", "/run/browser/sidecar.sock"))
    if conn is None:
        _write_all(stdout, encode({"t": "bye", "reason": "no_browser"}))
        return 0
    with conn, contextlib.suppress(OSError):  # the daemon went away, or the relay did (EPIPE)
        conn.sendall(HANDSHAKE)
        threading.Thread(target=_up, args=(conn, stdin), daemon=True).start()
        while chunk := conn.recv(CHUNK):
            _write_all(stdout, chunk)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
