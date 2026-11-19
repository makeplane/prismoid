# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# backend/prismoid/ai/frames/image_browser/sidecar/screencast.py
"""The daemon's screen mode. A connection whose first line is `{"verb": "screen"}`
stays open on the framed wire (`wire.py`):

    down  hello{v} · status{chromium, busy, origin, unreachable} · frame{seq, ts, w, h, fmt} + JPEG · reset
          · bye{reason}
    up    hello{v} · pointer{...} · key{...} · text{text} · back · hint{fps?, quality?, paused?, refresh?}
          · release (the relay's own: a person's control ended, so what their input holds is let go)

A screen is not a verb. It is never recorded or journaled, never moves the daemon's idle clock
(`_last_used`) and never releases a park `hold`; an open one does keep Chromium from the idle
stop, and a screen that finds Chromium stopped starts it under the verb lock (so a
viewer of a warm Machine never stares at nothing). Input is dropped while an agent's verb holds
that lock (`Daemon.busy`): the guest-side second guard behind the API relay's lease gate.
`status` also states the page's origin (`Browser.origin`), re-sent within a watch tick of a
navigation or a move to another tab: the address line a person reads before typing into the page;
and the origin the page could not load (`Browser.unreachable`, a refused proxy tunnel), for which a
person gets `back` - the page before, or the tab's opener.

ONE CDP screencast serves every connection. Each frame is encoded once and offered to every
connection's one-slot buffer (the newest replaces an unsent one, so a slow reader never holds up
the screencast or anyone else), and is acked no sooner than the fastest rate any connection asked
for, capped at `SCREENCAST_FPS_CAP`. Chromium sends nothing new until the last frame is acked, so
the rate is capped at the source."""

from __future__ import annotations

import asyncio
import contextlib
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NamedTuple, TypeGuard

from .browser import SCREENCAST_FPS_CAP, SCREENCAST_FPS_DEFAULT, SCREENCAST_QUALITY_DEFAULT
from .cdp import CdpClosed, CdpError
from .wire import INPUT_TYPES, PASTE_MAX, WIRE_VERSION, StreamDecoder, WireError, encode

if TYPE_CHECKING:
    from .daemon import Daemon

WATCH_S = 0.25  # how often an open screen looks at `busy`, `chromium` and restarts
RESTART_MIN_S = 1.0  # a refresh or a new quality restarts the screencast at most this often
RETRY_S = 5.0  # after a failed Chromium start for a screen, the wait before the next try
READ_CHUNK = 1 << 16
QUALITY_MAX = 100
RESET = encode({"t": "reset"})

# A CDP call refused mid-restart or mid-navigation (OSError covers ConnectionError and
# TimeoutError): the screen shrugs, and the next tick or the next event tries again.
_CDP_ERRORS = (CdpError, CdpClosed, OSError)


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _number(value: object) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


class _Status(NamedTuple):
    chromium: str  # running | stopped
    busy: bool  # an agent's verb holds the lock: a person's input is dropped
    origin: str  # the page's origin, "" for no web page
    unreachable: str  # the origin the page could not load (Chromium's error page), "" when it loaded


def _status_message(status: _Status) -> bytes:
    return encode({"t": "status", **status._asdict()})


@dataclass(eq=False)
class _Conn:
    """One screen connection: what it asked for, and what waits to be written to it."""

    writer: asyncio.StreamWriter
    fps: int = SCREENCAST_FPS_DEFAULT
    quality: int = SCREENCAST_QUALITY_DEFAULT
    paused: bool = False
    ready: bool = False  # set once hello and status are queued: no frame ever overtakes them
    live: bool = True  # False once it ends (or says bye): nothing more is queued for it
    drove: bool = False  # it sent a person's input: when it ends, what that input holds is let go
    control: list[bytes] = field(default_factory=list[bytes])  # hello, status, reset, bye: in order, all sent
    slot: bytes | None = None  # the newest unsent frame
    wake: asyncio.Event = field(default_factory=asyncio.Event)

    def post(self, message: bytes) -> None:
        if self.live:
            self.control.append(message)
            self.wake.set()

    def offer(self, frame: bytes) -> None:
        if self.live and self.ready and not self.paused:
            self.slot = frame
            self.wake.set()

    def bye(self, reason: str) -> None:
        """The parting message, and the last: the unsent frame goes and nothing more is queued,
        so whenever the writer next takes, nothing can follow the bye."""
        if self.live:
            self.slot = None
            self.post(encode({"t": "bye", "reason": reason}))
            self.live = False

    def take(self) -> list[bytes]:
        out, self.control = self.control, []
        if self.slot is not None:
            out.append(self.slot)
            self.slot = None
        return out


class ScreenHub:
    """Every screen connection of one daemon, and the one screencast they share."""

    def __init__(self, daemon: Daemon) -> None:
        self._daemon = daemon
        self._conns: set[_Conn] = set()
        self._cast_lock = asyncio.Lock()
        self._casting = False
        self._cast_quality = SCREENCAST_QUALITY_DEFAULT
        # (`daemon.starts`, `browser.page_gen`) when the screencast began: a restart is a new
        # page, and so is the page moving to a tab it opened (`Browser.follow`).
        self._cast_gen: tuple[int, int] | None = None
        self._cast_at = -math.inf  # when the screencast last started (the loop's clock)
        self._refresh = False
        self._seq = 0
        self._pending: int | None = None  # the newest frame's ack id, not yet acked
        self._ack_timer: asyncio.TimerHandle | None = None
        self._last_ack = 0.0
        self._status: _Status | None = None  # the last status every connection was sent
        self._page_closed = False  # the page's tab closed and the move off it has not run yet
        self._retry_at = 0.0
        self._watcher: asyncio.Task[None] | None = None
        self.dropped = 0  # input refused (busy, stopped, over PASTE_MAX): a count, never the input
        daemon.browser.on_screencast(self._on_frame)

    @property
    def active(self) -> bool:
        """Any screen open: the daemon's idle stop waits."""
        return bool(self._conns)

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One screen connection, from the line after `{"verb": "screen"}` until either side ends."""
        conn = _Conn(writer)
        self._conns.add(conn)  # `active` from here on: the idle stop must not race this connect
        pump = asyncio.create_task(self._pump(conn))
        try:
            conn.post(encode({"t": "hello", "v": WIRE_VERSION}))
            await self._try_start()
            status = self._current_status()
            if status != self._status:
                self._broadcast_status(status)
            else:
                conn.post(_status_message(status))
            conn.ready = True
            if self._watcher is None or self._watcher.done():
                self._watcher = asyncio.create_task(self._watch())
            # A first frame for the joiner: a still page sends none on its own, and a frame left
            # unacked while every other watcher is paused holds Chromium back. The screencast
            # restarts once (its start sends the current frame), and the waiting ack is re-timed.
            self._refresh = True
            self._reschedule_ack()
            await self._sync()
            await self._read(conn, reader)
        finally:
            conn.live = False
            self._conns.discard(conn)
            self._reschedule_ack()
            try:
                await self._close(conn, pump)  # first: a hung Chromium never holds a departing socket
            finally:
                if conn.drove:
                    await self._release()  # the relay's own `release` may have gone with its hop
                await self._sync()  # the last one out stops the screencast; a quality it alone held drops

    # ── reading: input and hints ──────────────────────────────────────────────────────────────

    async def _read(self, conn: _Conn, reader: asyncio.StreamReader) -> None:
        decoder = StreamDecoder()
        while True:
            try:
                data = await reader.read(READ_CHUNK)
            except OSError:
                return
            if not data:
                return
            try:
                messages = decoder.feed(data)
            except WireError:
                conn.bye("bad_message")
                return
            for header, _payload in messages:
                if not await self._handle(conn, header):
                    return

    async def _handle(self, conn: _Conn, header: dict[str, Any]) -> bool:
        """One message up; False ends the connection."""
        kind = header["t"]
        if kind == "hello" and header.get("v") != WIRE_VERSION:
            conn.bye("version")
            return False
        if kind == "hint":
            await self._hint(conn, header)
        elif kind in INPUT_TYPES:
            conn.drove = True
            await self._input(header)
        elif kind == "release":
            await self._release()
        return True  # anything else is a newer relay's message: ignored, never fatal

    async def _release(self) -> None:
        """Let go of whatever a person's input holds down (`Browser.release_all`): their control
        ended. Never dropped as busy - a key left down would act inside the agent's resumed verb."""
        if self._daemon.browser.alive:
            with contextlib.suppress(*_CDP_ERRORS):
                await self._daemon.browser.release_all()

    async def _hint(self, conn: _Conn, header: dict[str, Any]) -> None:
        """What this connection wants: `fps` (1..SCREENCAST_FPS_CAP), `quality` (1..100), `paused`
        (no frames, and no say in the ack rate) and `refresh` (one fresh frame now)."""
        fps, quality, paused = header.get("fps"), header.get("quality"), header.get("paused")
        if _is_int(fps):
            conn.fps = max(1, min(SCREENCAST_FPS_CAP, fps))
        if _is_int(quality):
            conn.quality = max(1, min(QUALITY_MAX, quality))
        if isinstance(paused, bool):
            conn.paused = paused
            if paused:
                conn.slot = None
        if header.get("refresh") is True:
            self._refresh = True
        self._reschedule_ack()
        await self._sync()

    async def _input(self, header: dict[str, Any]) -> None:
        """A person's pointer, key or paste, straight to the page: unless an agent's verb holds
        the lock (the guest-side second guard) or Chromium is down. Never recorded."""
        browser = self._daemon.browser
        if self._daemon.busy or not browser.alive:
            self.dropped += 1
            return
        kind = header["t"]
        if kind == "text":
            text = header.get("text")
            if not isinstance(text, str) or not text or len(text) > PASTE_MAX:
                self.dropped += 1
                return
        # Best effort: a page mid-navigation refuses some input, and nobody retries a mouse move.
        with contextlib.suppress(*_CDP_ERRORS, KeyError, IndexError):
            if kind == "pointer":
                await browser.pointer(header)
            elif kind == "key":
                await browser.key(header)
            elif kind == "back":
                await browser.go_back()
            else:
                await browser.insert_text(header["text"])

    # ── frames down, and the ack that paces them ──────────────────────────────────────────────

    def _on_frame(self, meta: dict[str, Any], jpeg: bytes, frame_ack_id: int) -> None:
        """From the CDP read loop, synchronously: encode once, offer the same bytes to every
        connection, then pace the ack."""
        self._pending = frame_ack_id
        if self._conns and jpeg:
            self._seq += 1
            header = {
                "t": "frame",
                "seq": self._seq,
                "ts": _number(meta.get("timestamp")),
                "w": round(_number(meta.get("deviceWidth"))),
                "h": round(_number(meta.get("deviceHeight"))),
                "fmt": "jpeg",
            }
            with contextlib.suppress(WireError):  # over MESSAGE_MAX: skipped, never queued
                message = encode(header, jpeg)
                for conn in self._conns:
                    conn.offer(message)
        self._schedule_ack()

    def _rate(self) -> int:
        """The fastest rate any watching connection asked for; 0 when every one is paused."""
        return max((c.fps for c in self._conns if not c.paused), default=0)

    def _schedule_ack(self) -> None:
        if self._pending is None or self._ack_timer is not None:
            return
        fps = self._rate()
        if fps <= 0:
            return  # nobody is looking: the frame waits unacked, and Chromium sends nothing more
        loop = asyncio.get_running_loop()
        delay = max(0.0, self._last_ack + 1.0 / fps - loop.time())
        self._ack_timer = loop.call_later(delay, self._ack)

    def _ack(self) -> None:
        self._ack_timer = None
        if self._pending is None:
            return
        frame_ack_id, self._pending = self._pending, None
        self._last_ack = asyncio.get_running_loop().time()
        self._daemon.browser.ack_frame(frame_ack_id)

    def _reschedule_ack(self) -> None:
        """The rates changed (a hint, a pause, a connection gone): the waiting ack is re-timed."""
        if self._ack_timer is not None:
            self._ack_timer.cancel()
            self._ack_timer = None
        self._schedule_ack()

    def _forget_pending(self) -> None:
        if self._ack_timer is not None:
            self._ack_timer.cancel()
            self._ack_timer = None
        self._pending = None

    # ── the screencast's life, and the status everyone is told ────────────────────────────────

    def _current_status(self) -> _Status:
        browser = self._daemon.browser
        return _Status(
            "running" if browser.alive else "stopped", self._daemon.busy, browser.origin, browser.unreachable
        )

    def _broadcast_status(self, status: _Status) -> None:
        self._status = status
        message = _status_message(status)
        for conn in self._conns:
            conn.post(message)

    async def _try_start(self) -> None:
        """Chromium for a screen, at most once per RETRY_S after a failure. Skipped
        while a verb holds the lock: that verb starts Chromium itself if it must."""
        loop = asyncio.get_running_loop()
        if self._daemon.busy or loop.time() < self._retry_at:
            return
        if not await self._daemon.ensure_browser():
            self._retry_at = loop.time() + RETRY_S

    async def _watch(self) -> None:
        """While any screen is open: keep Chromium up, follow a restart onto its new page, and say
        when `busy`, `chromium` or the page's origin changes. A poll: four looks a second cost
        nothing next to frames. It ends itself once the last screen has gone, never by
        cancellation: a tick may be starting Chromium, and a launch cancelled midway leaves a
        half-started process nothing reaps."""
        while True:
            await asyncio.sleep(WATCH_S)
            if not self._conns:
                return
            with contextlib.suppress(Exception):  # housekeeping never ends a screen; the next tick retries
                await self._tick()

    async def _tick(self) -> None:
        if not self._daemon.browser.running():
            await self._try_start()
        await self._daemon.follow_tab()  # a tab the page opened is what the screen shows
        self._drop_closed_page()
        await self._sync()
        status = self._current_status()
        if status != self._status:
            self._broadcast_status(status)

    async def _sync(self) -> None:
        """The screencast as the connections want it: running while any is open and Chromium is
        up, at the highest quality any asked for, and restarted once for `refresh` (a still page
        sends no frame on its own) or for a new quality - at most once per RESTART_MIN_S (the relay
        coalesces too, this is the guest's own guard). A restart
        asked for inside the window waits: `refresh` and the quality stay asked for, and the
        watch's first tick past the window restarts once for all of them. A screencast that
        starts on a page other than the last one's (a Chromium restart, however long it took, or
        a tab the page moved to) is preceded by `reset`."""
        async with self._cast_lock:
            browser = self._daemon.browser
            want = bool(self._conns) and browser.alive
            quality = max((c.quality for c in self._conns), default=self._cast_quality)
            try:
                if self._casting and self._cast_gen != self._gen():
                    self._casting = False  # the screencast went with the old page: nothing to stop
                    self._forget_pending()
                restart = self._casting and want and (self._refresh or quality != self._cast_quality)
                if restart and asyncio.get_running_loop().time() - self._cast_at < RESTART_MIN_S:
                    return  # too soon after the last start: the watch's tick restarts it later
                if self._casting and (not want or restart):
                    self._casting = False
                    self._forget_pending()
                    if browser.alive:
                        await browser.stop_screencast()
                if want and not self._casting:
                    gen = self._gen()
                    if self._cast_gen not in (None, gen):
                        self._reset()
                    self._refresh, self._cast_gen = False, gen  # told once, even if the start fails
                    self._cast_at = asyncio.get_running_loop().time()
                    await browser.start_screencast(quality=quality)
                    self._casting, self._cast_quality = True, quality
            except _CDP_ERRORS:
                self._casting = False  # the next tick tries again

    def _drop_closed_page(self) -> None:
        """The page's own tab closed while a verb holds the lock, so the move off it waits for the
        verb: every screen drops the closed tab's last frame now, once, instead of showing it for
        the verb's length (a 15 s `wait`, seen live). The move restarts the screen on the page."""
        browser = self._daemon.browser
        closed = browser.alive and not browser.page_open
        if closed and not self._page_closed:
            self._reset()
        self._page_closed = closed

    def _gen(self) -> tuple[int, int]:
        return (self._daemon.starts, self._daemon.browser.page_gen)

    def _reset(self) -> None:
        """A restarted Chromium, or a tab the page moved to, is a new page: what was queued from
        the old one goes, and every connection is told before the new page's first frame."""
        self._forget_pending()
        for conn in self._conns:
            conn.slot = None  # a frame of the old page never follows the reset
            conn.post(RESET)

    # ── writing ───────────────────────────────────────────────────────────────────────────────

    async def _pump(self, conn: _Conn) -> None:
        """This connection's writer: queued control messages in order, then the newest frame. A
        slow reader holds only its own slot."""
        with contextlib.suppress(OSError):
            while conn.live:
                await conn.wake.wait()
                conn.wake.clear()
                for message in conn.take():
                    conn.writer.write(message)
                await conn.writer.drain()

    async def _close(self, conn: _Conn, pump: asyncio.Task[None]) -> None:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
        conn.slot = None  # an unsent frame never follows the parting `bye`: bye is the last message
        with contextlib.suppress(OSError):  # a parting `bye` still goes out (a timeout is an OSError)
            for message in conn.take():
                conn.writer.write(message)
            await asyncio.wait_for(conn.writer.drain(), timeout=1.0)
        conn.writer.close()
        with contextlib.suppress(OSError):
            await conn.writer.wait_closed()
