# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# backend/prismoid/ai/frames/image_browser/sidecar/daemon.py
"""One Unix socket, one JSON line in, one JSON line out - except a screen (`{"verb": "screen"}`),
which stays open on the framed wire (`screencast.py`). Owns the browser's lifecycle (idle stop,
restart on death) and the step's recording (`recorder.Recorder` over the fd-held folder,
`recorder.DirSink`). Never logs a payload."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from .browser import IDLE_STOP_S, Browser
from .cdp import CdpClosed
from .recorder import DirSink, Recorder
from .screencast import ScreenHub
from .verbs import SNAPSHOT_CHARS_DEFAULT, VERBS, StaleReference


class Daemon:
    def __init__(self, *, browser: Browser, recording_dir: str) -> None:
        self.browser = browser
        # The step's recording: rows written as they land, into a folder
        # held by fd from the moment it is approved (`DirSink`).
        self.recorder = Recorder(DirSink(), recording_dir)
        self._last_used = time.monotonic()
        self._lock = asyncio.Lock()
        # `hold` (the engine, right before it parks a browser step on a gate whose TTL is far
        # longer than IDLE_STOP_S): the idle stop is suspended until the next non-`ping` verb
        # arrives (or an explicit `release`). Without it Chromium died under a parked gate and
        # the resumed click met a stale reference - and a second gate.
        self._held = False
        # Chromium starts that succeeded (the first, and every `_restart`): an open screen follows
        # a restart onto the new page by watching this move.
        self.starts = 0
        # The screen connections: not verbs, never recorded, never moving `_last_used`.
        self.screens = ScreenHub(self)

    @property
    def busy(self) -> bool:
        """A verb holds the browser (or a start does): a screen's input is dropped meanwhile."""
        return self._lock.locked()

    async def serve(
        self, sock_path: str, *, idle_s: float = IDLE_STOP_S, listening: Callable[[], None] | None = None
    ) -> None:
        await self.recorder.open()
        # Start Chromium (and hold the lock while doing it) before the socket exists at all, so
        # no client can connect and race dispatch()'s own "not alive -> start()" path - that race
        # was starting the browser twice.
        async with self._lock:
            await self.browser.start()
            self.starts += 1
        Path(sock_path).unlink(missing_ok=True)  # noqa: ASYNC240 - stdlib-only sidecar, one-shot at startup
        server = await asyncio.start_unix_server(self._client, path=sock_path)
        os.chmod(sock_path, 0o600)
        if listening is not None:
            listening()
        idle = asyncio.create_task(self._idle_loop(idle_s))
        try:
            async with server:
                await server.serve_forever()
        finally:
            idle.cancel()
            await self.browser.stop()

    async def _idle_loop(self, idle_s: float) -> None:
        while True:
            await asyncio.sleep(min(idle_s, 30.0))
            if self._idle(idle_s):
                async with self._lock:
                    if self._idle(idle_s):  # a screen may have opened, or a verb run, meanwhile
                        await self.browser.stop()

    def _idle(self, idle_s: float) -> bool:
        """Chromium may stop: no hold, no screen open, and no verb for `idle_s`."""
        return (
            self.browser.alive
            and not self._held
            and not self.screens.active
            and time.monotonic() - self._last_used > idle_s
        )

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            cmd = json.loads(line or b"{}")
            if not isinstance(cmd, dict):
                raise ValueError("request must be a JSON object")
        except (TimeoutError, json.JSONDecodeError, ValueError):
            writer.write(b'{"ok": false, "error": "bad request"}\n')
            await writer.drain()
            writer.close()
            return
        if cmd.get("verb") == "screen":
            # Not a verb: never dispatched, recorded or journaled, never moving
            # `_last_used` or a hold. The connection stays open on the framed wire.
            await self.screens.serve(reader, writer)
            return
        resp = await self.dispatch(cast("dict[str, Any]", cmd))
        writer.write(json.dumps(resp).encode() + b"\n")
        await writer.drain()
        writer.close()

    async def dispatch(self, cmd: dict[str, Any]) -> dict[str, Any]:
        verb = cmd.get("verb", "")
        if verb == "ping":
            # Not activity: idle means no tool call, and a health check is not a tool call.
            return {
                "ok": True,
                "chromium": "running" if self.browser.alive else "stopped",
                "held": self._held,
            }
        if verb == "record":
            # A step boundary, not a tool call: it neither counts as activity nor ends a hold. The
            # page stays warm; only the folder and its count are the new step's. A folder refused
            # (a symlink, another uid's) records nothing - said, never a fall back to the old one.
            if not (folder := str(cmd.get("dir") or "")):
                return {"ok": False, "error": "no recording folder named"}
            if await self.recorder.open(folder):
                return {"ok": True}
            return {"ok": False, "error": "recording folder unavailable"}
        self._last_used = time.monotonic()
        if verb in ("hold", "release"):
            # Daemon-level, never a browser verb: not in ACTING_VERBS, never recorded, never
            # journaled as an action. `hold` pins Chromium alive across a parked gate.
            self._held = verb == "hold"
            return {"ok": True, "held": self._held}
        self._held = False  # any real verb (the resume's first call) releases a hold
        if verb == "shutdown":
            asyncio.get_running_loop().call_later(0.1, _shutdown)
            return {"ok": True}
        if verb == "finalize":
            # Every error is a result (module docstring): `finalize` does real filesystem work
            # (symlink-safe opens, an atomic rename) outside the browser lock's own try/except,
            # so it needs the same "never a crashed connection" guarantee here.
            try:
                return await self.recorder.finalize()
            except Exception as e:
                return {"ok": False, "error": type(e).__name__}
        try:
            folder = self.recorder.folder  # the step this act belongs to
            async with self._lock:
                if not self.browser.alive and not await self._restart():
                    return {"ok": False, "error": "browser failed to start"}
                try:
                    if await self.browser.follow():  # a tab the page opened: this verb acts there
                        self.recorder.forget_targets()
                    result = await self.browser.handle(cmd)
                except StaleReference as s:
                    return {"ok": False, "error": f"stale reference {s.ref}; take a new snapshot"}
                except (CdpClosed, ConnectionError):
                    # A crash-like failure always means "browser restarted", even for a verb we
                    # would otherwise not recognize - the verb never got a chance to run either way.
                    if not await self._restart(stop_first=True):
                        return {"ok": False, "error": "browser failed to start"}
                    return {"ok": False, "error": "browser restarted"}
            if verb not in VERBS:
                return {"ok": False, "error": f"unknown verb {verb!r}"}
            record = result.pop("_record", None)
            # A step boundary that moved the recording while this ran (a call its driver gave up
            # on) leaves the act the last step's: it never lands in the new step's trail.
            if self.recorder.folder == folder:
                await self.recorder.file(verb, cmd, result, record)
            # A snapshot, or an action that brought the page back: the next action's target is
            # named from this tree - only after this action was named from the one it acted on.
            if (targets := result.pop("_targets", None)) is not None:
                self.recorder.remember_targets(targets)
            return result
        except Exception as e:
            # The protocol's whole point: every error is a result, never a crashed connection.
            return {"ok": False, "error": type(e).__name__}

    async def _restart(self, *, stop_first: bool = False) -> bool:
        """Best-effort (re)start. Always clears remembered element targets - a restarted browser's
        refs are gone too (Browser.start() clears those). Returns whether it worked."""
        if stop_first:
            await self.browser.stop()
        self.recorder.forget_targets()
        try:
            await self.browser.start()
        except Exception:
            return False
        self.starts += 1
        return True

    async def ensure_browser(self) -> bool:
        """Start Chromium for a screen when it is stopped, or died between verbs.
        Under the verb lock and through `_restart`, the path a verb takes, so the two never start
        it twice. It never moves `_last_used` or releases a hold: watching is not a tool call."""
        if self.browser.running():
            return True
        async with self._lock:
            if self.browser.running():
                return True
            return await self._restart()

    async def follow_tab(self) -> None:
        """An open screen's watch moves the page to a tab it opened, so the screen shows that tab
        with no verb coming (`Browser.follow`). Under the verb lock, like a verb, and only when a
        move is pending: taking the lock reads as "agent driving" on every screen. Skipped while
        a verb holds it; the next tick or the next verb moves it."""
        if self.busy or not self.browser.running() or not self.browser.follow_pending:
            return
        async with self._lock:
            if await self.browser.follow():
                self.recorder.forget_targets()


def _listening() -> None:
    """Tell the `ctl` that started this daemon its socket listens: stdout, its pipe, closes (nothing is written there)."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.close(devnull)


def _shutdown() -> None:
    raise SystemExit(0)


def main() -> None:
    sock = os.environ.get("SIDECAR_SOCK", "/run/browser/sidecar.sock")
    profile = os.environ.get("SIDECAR_PROFILE", "/browser/profile")
    recording = os.environ.get("SIDECAR_RECORDING", "/artifacts/recording-1")
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or None
    idle = float(os.environ.get("SIDECAR_IDLE_S", str(IDLE_STOP_S)))
    snapshot_chars = int(os.environ.get("SIDECAR_SNAPSHOT_CHARS", str(SNAPSHOT_CHARS_DEFAULT)))
    headed_ua = os.environ.get("SIDECAR_HEADED_UA") == "1"
    Path(profile).mkdir(parents=True, exist_ok=True)
    asyncio.run(
        Daemon(
            browser=Browser(
                profile_dir=profile, proxy=proxy, snapshot_chars=snapshot_chars, headed_ua=headed_ua
            ),
            recording_dir=recording,
        ).serve(sock, idle_s=idle, listening=_listening)
    )


if __name__ == "__main__":
    main()
