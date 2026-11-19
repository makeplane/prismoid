# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# backend/tests/unit/ai/frames/image_browser/test_browser_sidecar_screen.py
"""The sidecar's screen mode: the wire's stdlib twin, the screencast and input on `Browser`, the daemon's
ScreenHub over its real socket, and the exec bridge. No Chromium: a real `Browser` over `FakeCdp`."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from prismoid.schemas import screen as host
from tests.support.fakes.cdp_scripts import FakeCdp
from tests.support.paths import BACKEND

SIDECAR = BACKEND / "prismoid" / "ai" / "frames" / "image_browser"
sys.path.insert(0, str(SIDECAR))

from sidecar.browser import Browser  # noqa: E402
from sidecar.daemon import Daemon  # noqa: E402
from sidecar.screencast import _Conn  # noqa: E402  # pyright: ignore[reportPrivateUsage]
from sidecar.wire import HEADER_MAX, MESSAGE_MAX, PASTE_MAX, StreamDecoder, WireError, encode  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0 a frame"
META: dict[str, Any] = {
    "deviceWidth": 1280,
    "deviceHeight": 900,
    "offsetTop": 0,
    "pageScaleFactor": 1,
    "scrollOffsetX": 0,
    "scrollOffsetY": 0,
    "timestamp": 1727.5,
}
CLICK: dict[str, Any] = {
    "t": "pointer",
    "type": "down",
    "x": 100,
    "y": 200,
    "button": "left",
    "buttons": 1,
    "clicks": 1,
}

# ── the wire's twin ─────────────────────────────────────────────────────────────────────────────

CASES: list[tuple[dict[str, Any], bytes]] = [
    ({"t": "hello", "v": 1}, b""),
    ({"t": "frame", "seq": 7, "ts": 1727.25, "w": 1280, "h": 900, "fmt": "jpeg"}, JPEG),
    ({"t": "status", "chromium": "running", "busy": False}, b""),
    ({"t": "text", "text": "pässwörd, パスワード ✓"}, b""),
    ({"t": "pointer", "type": "wheel", "x": 640.5, "y": 450, "dx": 0, "dy": -600}, b""),
    ({"t": "bye", "reason": "no_browser"}, b""),
]


def test_wire_is_the_host_codec_byte_for_byte() -> None:
    """The image cannot import `prismoid.schemas.screen`, so `sidecar/wire.py` is its stdlib copy:
    the same input gives the same bytes, and both decoders read the stream the same way."""
    for header, payload in CASES:
        assert encode(header, payload) == host.encode(header, payload), header["t"]
    stream = b"".join(encode(h, p) for h, p in CASES)
    ours, theirs = StreamDecoder(), host.StreamDecoder()
    got_ours: list[tuple[dict[str, Any], bytes]] = []
    got_theirs: list[tuple[dict[str, Any], bytes]] = []
    for i in range(0, len(stream), 7):  # an awkward split: headers and payloads cut mid-way
        got_ours += ours.feed(stream[i : i + 7])
        got_theirs += theirs.feed(stream[i : i + 7])
    assert got_ours == got_theirs == [({**h, "n": len(p)}, p) for h, p in CASES]


def test_wire_decoder_refuses_what_the_wire_forbids() -> None:
    with pytest.raises(WireError):
        StreamDecoder().feed((HEADER_MAX + 1).to_bytes(4, "big"))  # a header over 4096 bytes
    with pytest.raises(WireError):
        StreamDecoder().feed((5).to_bytes(4, "big") + b"nope!")  # not JSON
    with pytest.raises(WireError):
        StreamDecoder().feed((2).to_bytes(4, "big") + b"[]")  # not an object with a type
    too_big = json.dumps({"t": "frame", "n": MESSAGE_MAX}).encode()
    with pytest.raises(WireError):
        StreamDecoder().feed(len(too_big).to_bytes(4, "big") + too_big)
    with pytest.raises(WireError):
        encode({"t": "text", "text": "x" * HEADER_MAX})


def _framed(head: bytes) -> bytes:
    return len(head).to_bytes(4, "big") + head


@pytest.mark.parametrize(
    "bad",
    [
        _framed('{"t":"reset"}'.encode("utf-16")),  # UTF-16 behind its byte-order mark
        _framed('{"t":"reset"}'.encode("utf-16-le")),  # ... and without one
        _framed(b"\xef\xbb\xbf" + b'{"t":"reset"}'),  # UTF-8 behind a byte-order mark
        _framed(b'{"t":"\xed\xa0\x80"}'),  # a lone surrogate: never strict UTF-8
        _framed(b'{"t":"frame","n":-1}'),
        (HEADER_MAX + 1).to_bytes(4, "big"),
    ],
    ids=["utf16-bom", "utf16-le", "utf8-bom", "surrogate", "negative-n", "header-over-max"],
)
def test_wire_decoders_refuse_the_same_streams_and_stay_broken(bad: bytes) -> None:
    """A header is strict UTF-8 on both sides (`json.loads` on bytes would guess UTF-16/32 and
    skip a BOM), and after a refusal both decoders stay broken: the stream is desynced for good."""
    ours, theirs = StreamDecoder(), host.StreamDecoder()
    with pytest.raises(WireError):
        ours.feed(bad)
    with pytest.raises(WireError):
        ours.feed(encode({"t": "reset"}))
    with pytest.raises(host.WireError):
        theirs.feed(bad)
    with pytest.raises(host.WireError):
        theirs.feed(host.encode({"t": "reset"}))


# ── Browser: the screencast and input, over FakeCdp ─────────────────────────────────────────────


@pytest.fixture
def wired() -> tuple[Browser, FakeCdp]:
    b = Browser(profile_dir="/nonexistent", proxy=None)
    cdp = FakeCdp()
    b.connect(cdp)
    b.verbs._session = "S1"  # pyright: ignore[reportPrivateUsage]
    b.alive = True
    return b, cdp


def _frame_event(frame_ack_id: int, data: str | None = None) -> dict[str, Any]:
    encoded = base64.b64encode(JPEG).decode() if data is None else data
    return {"data": encoded, "metadata": META, "sessionId": frame_ack_id}


async def test_cdp_screencast_is_jpeg_at_the_native_viewport(
    wired: tuple[Browser, FakeCdp],
) -> None:
    b, cdp = wired
    await b.start_screencast(quality=60)
    await b.stop_screencast()
    assert cdp.sent("Page.startScreencast") == [
        {"format": "jpeg", "quality": 60, "maxWidth": 1280, "maxHeight": 900, "everyNthFrame": 1}
    ]
    assert cdp.sent("Page.stopScreencast") == [None]


async def test_cdp_frame_reaches_the_screen_decoded_and_is_acked(
    wired: tuple[Browser, FakeCdp],
) -> None:
    b, cdp = wired
    seen: list[tuple[dict[str, Any], bytes, int]] = []
    b.on_screencast(lambda meta, jpeg, ack_id: seen.append((meta, jpeg, ack_id)))
    cdp.emit("Page.screencastFrame", _frame_event(7))
    cdp.emit("Page.screencastFrame", _frame_event(8, data="%%% not base64"))
    # The undecodable frame still reaches the screen, empty, so it is acked and Chromium never stalls.
    assert seen == [(META, JPEG, 7), (META, b"", 8)]
    b.ack_frame(7)
    await cdp.drain()
    assert cdp.sent("Page.screencastFrameAck") == [{"sessionId": 7}]


async def test_cdp_frame_is_acked_at_once_when_no_screen_is_registered(
    wired: tuple[Browser, FakeCdp],
) -> None:
    _b, cdp = wired
    cdp.emit("Page.screencastFrame", _frame_event(3))
    await cdp.drain()
    assert cdp.sent("Page.screencastFrameAck") == [{"sessionId": 3}]


async def test_cdp_pointer_events_map_onto_dispatch_mouse_event(wired: tuple[Browser, FakeCdp]) -> None:
    b, cdp = wired
    await b.pointer({"type": "move", "x": 10, "y": 20})
    await b.pointer(
        {"type": "down", "x": 100.5, "y": 200, "button": "left", "buttons": 1, "clicks": 2, "modifiers": 8}
    )
    await b.pointer({"type": "up", "x": 100.5, "y": 200, "button": "left", "buttons": 0, "clicks": 2})
    await b.pointer({"type": "wheel", "x": 640, "y": 450, "dx": 0, "dy": -600})
    await b.pointer({"type": "down", "x": float("nan"), "y": 99999, "button": ["left"], "clicks": 40})
    await b.pointer({"type": "drag", "x": 1, "y": 1})  # not a pointer type: nothing is sent
    assert cdp.sent("Input.dispatchMouseEvent") == [
        {"type": "mouseMoved", "x": 10.0, "y": 20.0, "button": "none", "buttons": 0, "modifiers": 0},
        {
            "type": "mousePressed",
            "x": 100.5,
            "y": 200.0,
            "button": "left",
            "buttons": 1,
            "modifiers": 8,
            "clickCount": 2,
        },
        {
            "type": "mouseReleased",
            "x": 100.5,
            "y": 200.0,
            "button": "left",
            "buttons": 0,
            "modifiers": 0,
            "clickCount": 2,
        },
        {
            "type": "mouseWheel",
            "x": 640.0,
            "y": 450.0,
            "button": "none",
            "buttons": 0,
            "modifiers": 0,
            "deltaX": 0.0,
            "deltaY": -600.0,
        },
        # Hostile numbers are clamped, an unknown button is "none": CDP never sees NaN.
        {
            "type": "mousePressed",
            "x": 0.0,
            "y": 900.0,
            "button": "none",
            "buttons": 0,
            "modifiers": 0,
            "clickCount": 3,
        },
    ]


async def test_cdp_key_events_map_onto_dispatch_key_event(wired: tuple[Browser, FakeCdp]) -> None:
    b, cdp = wired
    await b.key({"type": "down", "key": "a", "code": "KeyA", "text": "a", "keyCode": 65})
    await b.key({"type": "up", "key": "a", "code": "KeyA", "keyCode": 65})
    await b.key({"type": "down", "key": "Backspace", "code": "Backspace", "keyCode": 8})
    await b.key({"type": "down", "key": "Enter", "code": "Enter"})
    await b.key({"type": "press", "key": "a"})  # not a key type: nothing is sent
    a = {"key": "a", "code": "KeyA", "modifiers": 0, "windowsVirtualKeyCode": 65}
    assert cdp.sent("Input.dispatchKeyEvent") == [
        {**a, "type": "keyDown", "text": "a", "unmodifiedText": "a"},
        {**a, "type": "keyUp"},
        {
            "key": "Backspace",
            "code": "Backspace",
            "modifiers": 0,
            "windowsVirtualKeyCode": 8,
            "type": "rawKeyDown",
        },
        {
            "key": "Enter",
            "code": "Enter",
            "modifiers": 0,
            "windowsVirtualKeyCode": 13,
            "type": "keyDown",
            "text": "\r",
            "unmodifiedText": "\r",
        },
    ]


async def test_cdp_paste_is_typed_with_insert_text(wired: tuple[Browser, FakeCdp]) -> None:
    b, cdp = wired
    await b.insert_text("hunter2")
    assert cdp.sent("Input.insertText") == [{"text": "hunter2"}]


# ── the daemon's ScreenHub, over its real socket ────────────────────────────────────────────────


class CdpBrowser(Browser):
    """A real `Browser` (its screencast and input code is what runs) whose every start is a fresh
    `FakeCdp` page instead of a Chromium."""

    def __init__(self) -> None:
        super().__init__(profile_dir="/nonexistent", proxy=None)
        self.pages: list[FakeCdp] = []
        self.acks: list[float] = []
        self.repaint = False  # every ack is followed by a new frame, as a busy page does
        self.lock: asyncio.Lock | None = None
        self.locked_at_start: list[bool] = []
        # Knobs a test turns after the first start, each modelling a real Chromium's way:
        self.restart_s = 0.0  # a relaunch that takes this long (`alive` stays False meanwhile)
        self.launching = False  # a start began and has not finished (left True if it was cancelled)
        self.paint_on_cast = False  # `Page.startScreencast` sends the page's current frame at once
        self.paint_on_pointer = False  # a click repaints the page as it lands
        self.stop_cast_s = 0.0  # `Page.stopScreencast` waits this long on the pipe
        self.stop_cast_error: Exception | None = None  # ... or fails outright
        self._frame_ids = itertools.count(1)

    @property
    def cdp(self) -> FakeCdp:
        return self.pages[-1]

    async def start(self) -> None:
        if self.lock is not None:
            self.locked_at_start.append(self.lock.locked())
        self.launching = True
        if self.pages and self.restart_s:
            await asyncio.sleep(self.restart_s)
        page = FakeCdp()
        verbs = self.connect(page)
        verbs._session = "S1"  # pyright: ignore[reportPrivateUsage]
        # one tab, as a real start leaves it
        verbs._target, verbs._tabs = "T1", {"T1": "S1"}  # pyright: ignore[reportPrivateUsage]
        self.pages.append(page)
        self.alive = True
        self.launching = False

    async def start_screencast(self, *, quality: int) -> None:
        await super().start_screencast(quality=quality)
        if self.paint_on_cast:
            self.paint()

    async def stop_screencast(self) -> None:
        if self.stop_cast_s:
            await asyncio.sleep(self.stop_cast_s)
        if self.stop_cast_error is not None:
            raise self.stop_cast_error
        await super().stop_screencast()

    async def pointer(self, ev: Mapping[str, Any]) -> None:
        await super().pointer(ev)
        if self.paint_on_pointer:
            self.paint()

    async def stop(self) -> None:
        self.alive = False

    async def handle(self, cmd: dict[str, Any]) -> dict[str, Any]:
        if cmd.get("verb") == "die":
            self.alive = False
            raise ConnectionError("pipe closed")
        return {"ok": True, "url": "http://x/", "title": "X"}

    def ack_frame(self, frame_ack_id: int) -> None:
        super().ack_frame(frame_ack_id)
        self.acks.append(time.monotonic())
        if self.repaint:
            asyncio.get_running_loop().call_soon(self.paint)

    def paint(self) -> None:
        """Chromium repainting the page: one `Page.screencastFrame`."""
        self.cdp.emit("Page.screencastFrame", _frame_event(next(self._frame_ids)))


ScreenDaemon = tuple[str, CdpBrowser, Path, Daemon]


async def _serve(tmp_path: Path, idle_s: float = 60.0) -> tuple[ScreenDaemon, asyncio.Task[None], str]:
    # AF_UNIX paths are capped at ~104 bytes on macOS, so the socket gets a short /tmp directory
    # (the daemon suite's reason, test_browser_sidecar_daemon.py).
    sock_dir = tempfile.mkdtemp(dir="/tmp", prefix="sc-")
    browser = CdpBrowser()
    d = Daemon(browser=browser, recording_dir=str(tmp_path / "rec"))
    sock = str(Path(sock_dir) / "s.sock")
    task = asyncio.create_task(d.serve(sock, idle_s=idle_s))
    for _ in range(100):
        if Path(sock).exists():  # noqa: ASYNC240 - test polling, no trio/anyio in this repo
            break
        await asyncio.sleep(0.02)
    return (sock, browser, tmp_path / "rec", d), task, sock_dir


@pytest.fixture
async def screen_daemon(tmp_path: Path) -> AsyncIterator[ScreenDaemon]:
    served, task, sock_dir = await _serve(tmp_path)
    yield served
    task.cancel()
    shutil.rmtree(sock_dir, ignore_errors=True)


class Screen:
    """The relay's end of a screen connection: framed messages both ways."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer
        self._decoder = StreamDecoder()
        self._ready: deque[tuple[dict[str, Any], bytes]] = deque()

    @classmethod
    async def open(cls, sock: str) -> Screen:
        reader, writer = await asyncio.open_unix_connection(sock)
        writer.write(b'{"verb": "screen"}\n')
        await writer.drain()
        return cls(reader, writer)

    async def recv(self, within: float = 3.0) -> tuple[dict[str, Any], bytes]:
        async def next_message() -> tuple[dict[str, Any], bytes]:
            while not self._ready:
                data = await self.reader.read(1 << 16)
                if not data:
                    raise EOFError("the screen ended")
                self._ready.extend(self._decoder.feed(data))
            return self._ready.popleft()

        return await asyncio.wait_for(next_message(), within)

    async def until(self, t: str, within: float = 3.0) -> tuple[dict[str, Any], bytes]:
        deadline = time.monotonic() + within
        while True:
            header, payload = await self.recv(max(0.01, deadline - time.monotonic()))
            if header["t"] == t:
                return header, payload

    async def send(self, header: dict[str, Any], payload: bytes = b"") -> None:
        self.writer.write(encode(header, payload))
        await self.writer.drain()

    async def close(self) -> None:
        self.writer.close()
        with contextlib.suppress(OSError):
            await self.writer.wait_closed()


async def _eventually(check: Callable[[], object], within: float = 3.0) -> None:
    for _ in range(int(within / 0.01)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition never held")


async def _verb(sock: str, cmd: dict[str, Any]) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(sock)
    writer.write(json.dumps(cmd).encode() + b"\n")
    await writer.drain()
    line = await reader.readline()
    writer.close()
    return json.loads(line)


def _casts(cdp: FakeCdp) -> list[str]:
    """One page's screencast life, as `start@<quality>` and `stop`."""
    out: list[str] = []
    for method, params in cdp.calls:
        if method == "Page.startScreencast":
            out.append(f"start@{(params or {})['quality']}")
        elif method == "Page.stopScreencast":
            out.append("stop")
    return out


async def test_screen_says_hello_then_status_and_streams_framed_jpegs(screen_daemon: ScreenDaemon) -> None:
    sock, browser, _, _ = screen_daemon
    screen = await Screen.open(sock)
    try:
        hello, _ = await screen.recv()
        status, _ = await screen.recv()
        assert hello == {"t": "hello", "v": 1, "n": 0}
        assert status == {
            "t": "status",
            "chromium": "running",
            "busy": False,
            "origin": "",
            "unreachable": "",
            "n": 0,
        }
        await _eventually(lambda: _casts(browser.cdp) == ["start@60"])
        browser.paint()
        header, jpeg = await screen.until("frame")
        assert header == {
            "t": "frame",
            "seq": 1,
            "ts": 1727.5,
            "w": 1280,
            "h": 900,
            "fmt": "jpeg",
            "n": len(JPEG),
        }
        assert jpeg == JPEG and jpeg.startswith(b"\xff\xd8")  # the payload is the JPEG, as CDP sent it
        await _eventually(lambda: browser.cdp.sent("Page.screencastFrameAck") == [{"sessionId": 1}])
    finally:
        await screen.close()
    await _eventually(lambda: _casts(browser.cdp) == ["start@60", "stop"])  # the last one out stops it


async def test_screen_acks_are_paced_at_the_fastest_rate_asked_and_capped_at_thirty(
    screen_daemon: ScreenDaemon,
) -> None:
    sock, browser, _, _ = screen_daemon
    screen = await Screen.open(sock)
    try:
        await screen.until("status")
        browser.repaint = True
        browser.paint()
        await asyncio.sleep(0.5)
        # The default is a thumbnail's 1 fps: the first frame is acked at once, the next not
        # before a second has passed, so Chromium sends nothing more meanwhile.
        assert len(browser.acks) == 1
        await screen.send({"t": "hint", "fps": 50})  # over the cap: held to 30
        await asyncio.sleep(0.6)
        paced = browser.acks[1:]
        assert 9 <= len(paced) <= 19, paced  # 30 fps is 18 in 0.6 s; 10 fps would be 6, 50 would be 30
        assert all(b - a >= 0.03 for a, b in itertools.pairwise(paced)), paced
        await screen.send({"t": "hint", "paused": True})
        await asyncio.sleep(0.1)
        settled = len(browser.acks)
        await asyncio.sleep(0.4)
        assert len(browser.acks) == settled  # paused: no ack, so no new frame either
    finally:
        await screen.close()


async def test_screen_quality_follows_the_highest_ask_and_refresh_restarts_once(
    screen_daemon: ScreenDaemon,
) -> None:
    sock, browser, _, _ = screen_daemon
    a = await Screen.open(sock)
    b: Screen | None = None
    try:
        await a.until("status")
        await _eventually(lambda: _casts(browser.cdp) == ["start@60"])
        b = await Screen.open(sock)
        await b.until("status")
        # A joiner restarts the screencast once, so the page's current frame reaches it.
        await _eventually(lambda: _casts(browser.cdp) == ["start@60", "stop", "start@60"])
        await b.send({"t": "hint", "quality": 80})
        await _eventually(lambda: _casts(browser.cdp)[3:] == ["stop", "start@80"])
        await a.send({"t": "hint", "refresh": True})  # a still page sends no frame on its own
        await _eventually(lambda: _casts(browser.cdp)[5:] == ["stop", "start@80"])
        await b.close()  # the only one who asked for 80 left
        await _eventually(lambda: _casts(browser.cdp)[7:] == ["stop", "start@60"])
    finally:
        await a.close()
        if b is not None:
            await b.close()
    await _eventually(lambda: _casts(browser.cdp)[9:] == ["stop"])


async def test_screen_restarts_the_screencast_at_most_once_a_second(screen_daemon: ScreenDaemon) -> None:
    """The sidecar's own guard behind the relay's: a refresh or a new quality
    restarts the screencast, and restarts closer than a second apart are not made - the asks
    inside the window wait, and the next restart after it carries all of them, once."""
    sock, browser, _, _ = screen_daemon
    a = await Screen.open(sock)
    try:
        await a.until("status")
        await _eventually(lambda: _casts(browser.cdp) == ["start@60"])
        mark = len(_casts(browser.cdp))
        await asyncio.sleep(1.1)  # the join's own start is a second behind us
        await a.send({"t": "hint", "refresh": True})
        await _eventually(lambda: _casts(browser.cdp)[mark:] == ["stop", "start@60"])
        started = time.monotonic()
        for _ in range(5):
            await a.send({"t": "hint", "refresh": True})
        await a.send({"t": "hint", "quality": 70})
        await asyncio.sleep(0.5)
        assert _casts(browser.cdp)[mark:] == ["stop", "start@60"], "nothing more inside the second"
        await _eventually(lambda: _casts(browser.cdp)[mark:] == ["stop", "start@60", "stop", "start@70"])
        assert time.monotonic() - started >= 0.8
        await asyncio.sleep(1.2)
        assert len(_casts(browser.cdp)) == mark + 4, "six asks, one restart"
    finally:
        await a.close()


async def test_screen_input_is_dropped_while_a_verb_holds_the_lock_and_status_says_busy(
    screen_daemon: ScreenDaemon,
) -> None:
    sock, browser, _, d = screen_daemon
    screen = await Screen.open(sock)
    try:
        status, _ = await screen.until("status")
        assert status["busy"] is False
        lock = d._lock  # pyright: ignore[reportPrivateUsage]
        await lock.acquire()  # an agent's verb in flight
        try:
            busy, _ = await screen.until("status")
            assert busy["busy"] is True
            await screen.send(CLICK)
            await screen.send({"t": "text", "text": "hunter2"})
            await _eventually(lambda: d.screens.dropped == 2)
            assert not browser.cdp.sent("Input.dispatchMouseEvent")
            assert not browser.cdp.sent("Input.insertText")
        finally:
            lock.release()
        idle, _ = await screen.until("status")
        assert idle["busy"] is False
        await screen.send(CLICK)
        await _eventually(lambda: browser.cdp.sent("Input.dispatchMouseEvent"))
        assert browser.cdp.sent("Input.dispatchMouseEvent") == [
            {
                "type": "mousePressed",
                "x": 100.0,
                "y": 200.0,
                "button": "left",
                "buttons": 1,
                "modifiers": 0,
                "clickCount": 1,
            }
        ]
    finally:
        await screen.close()


async def test_screen_paste_lands_as_typed_text_and_one_over_the_cap_is_dropped(
    screen_daemon: ScreenDaemon,
) -> None:
    sock, browser, _, d = screen_daemon
    screen = await Screen.open(sock)
    try:
        await screen.until("status")
        await screen.send({"t": "text", "text": "x" * (PASTE_MAX + 1)})
        await screen.send({"t": "text", "text": "hunter2"})
        await _eventually(lambda: browser.cdp.sent("Input.insertText"))
        assert browser.cdp.sent("Input.insertText") == [{"text": "hunter2"}]
        assert d.screens.dropped == 1
    finally:
        await screen.close()


async def test_screen_never_moves_the_idle_clock_releases_a_hold_or_records(
    screen_daemon: ScreenDaemon,
) -> None:
    sock, browser, rec, d = screen_daemon
    assert (await _verb(sock, {"verb": "hold"}))["held"] is True
    before = d._last_used  # pyright: ignore[reportPrivateUsage]
    screen = await Screen.open(sock)
    try:
        await screen.until("status")
        await screen.send({"t": "hint", "fps": 10, "refresh": True})
        await screen.send(CLICK)
        await screen.send({"t": "key", "type": "down", "key": "a", "code": "KeyA", "text": "a"})
        await screen.send({"t": "text", "text": "hunter2"})
        browser.paint()
        await screen.until("frame")
        await _eventually(lambda: browser.cdp.sent("Input.insertText"))
    finally:
        await screen.close()
    assert d._last_used == before  # pyright: ignore[reportPrivateUsage]
    assert d._held is True  # pyright: ignore[reportPrivateUsage]
    assert json.loads((rec / "actions.json").read_text()) == []
    assert sorted(p.name for p in rec.iterdir()) == ["actions.json"]  # no frame, no input, filed


async def test_screen_open_keeps_chromium_from_the_idle_stop(tmp_path: Path) -> None:
    (sock, browser, _, d), task, sock_dir = await _serve(tmp_path, idle_s=0.05)
    try:
        screen = await Screen.open(sock)
        await screen.until("status")
        starts = d.starts
        await asyncio.sleep(0.4)  # eight idle deadlines
        # Kept: never stopped (and so never restarted by the screen either).
        assert browser.alive and d.starts == starts
        await screen.close()
        await _eventually(lambda: not browser.alive)  # watching alone keeps nothing: the idle stop is back
    finally:
        task.cancel()
        shutil.rmtree(sock_dir, ignore_errors=True)


async def test_screen_starts_a_stopped_chromium_under_the_verb_lock(screen_daemon: ScreenDaemon) -> None:
    sock, browser, _, d = screen_daemon
    await browser.stop()  # the daemon's idle stop, say, before anyone opened the screen
    browser.lock = d._lock  # pyright: ignore[reportPrivateUsage]
    starts = d.starts
    screen = await Screen.open(sock)
    try:
        hello, _ = await screen.recv()
        status, _ = await screen.recv()
        assert hello["t"] == "hello" and status["t"] == "status" and status["chromium"] == "running"
        assert browser.alive and d.starts == starts + 1 and browser.locked_at_start == [True]
    finally:
        await screen.close()


async def test_screen_follows_a_tab_the_page_opens_with_a_reset_and_a_screencast_there(
    screen_daemon: ScreenDaemon,
) -> None:
    """Found live: a `target=_blank` link put a new tab in front, the screen stayed on the page it
    had, hidden, and froze. An open screen follows the new tab with no verb coming: `reset`, the
    old tab's screencast stopped, a new one on the tab, and that tab's frames reach the screen."""
    sock, browser, _, _d = screen_daemon
    screen = await Screen.open(sock)
    try:
        await screen.until("status")
        cdp = browser.cdp
        await _eventually(lambda: _casts(cdp) == ["start@60"])
        cdp.answer("Page.getFrameTree", {"frameTree": {"frame": {"id": "F2"}}})
        info = {"targetId": "T2", "type": "page", "openerId": "T1"}
        cdp.emit(
            "Target.attachedToTarget", {"sessionId": "S2", "targetInfo": info, "waitingForDebugger": True}
        )
        await screen.until("reset")
        await _eventually(lambda: cdp.sent_on("Page.startScreencast") == ["S1", "S2"])
        assert "S1" in cdp.sent_on("Page.stopScreencast")
        cdp.emit("Page.screencastFrame", _frame_event(41), session="S2")
        _, jpeg = await screen.until("frame")
        assert jpeg == JPEG
    finally:
        await screen.close()


async def _origin(screen: Screen, origin: str) -> None:
    """Statuses until one states `origin`."""
    while (await screen.until("status"))[0]["origin"] != origin:
        pass


async def test_screen_status_states_the_pages_origin_on_navigation_and_on_a_tab_move(
    screen_daemon: ScreenDaemon,
) -> None:
    """The address line a person decides on: the page's origin as Chromium reports
    its tab, re-sent when the page navigates and when it moves to a tab it opened."""
    sock, browser, _, _d = screen_daemon
    screen = await Screen.open(sock)
    try:
        first, _ = await screen.until("status")
        assert first["origin"] == ""
        cdp = browser.cdp
        nav = {"targetId": "T1", "type": "page", "url": "https://accounts.example.com/login?next=/x"}
        cdp.emit("Target.targetInfoChanged", {"targetInfo": nav})
        await _origin(screen, "https://accounts.example.com")
        cdp.answer("Page.getFrameTree", {"frameTree": {"frame": {"id": "F2"}}})
        opened = {"targetId": "T2", "type": "page", "openerId": "T1", "url": "https://sso.example.org/"}
        cdp.emit(
            "Target.attachedToTarget", {"sessionId": "S2", "targetInfo": opened, "waitingForDebugger": True}
        )
        await screen.until("reset")
        await _origin(screen, "https://sso.example.org")
    finally:
        await screen.close()


async def test_screen_status_says_which_origin_the_page_could_not_reach_and_back_leaves_it(
    screen_daemon: ScreenDaemon,
) -> None:
    """A navigation the egress proxy refuses lands on Chromium's own error page (seen live: a
    person driving between steps): `status` names the origin it could not reach, and the screen's
    Back takes the page to the one before."""
    sock, browser, _, _d = screen_daemon
    screen = await Screen.open(sock)
    try:
        first, _ = await screen.until("status")
        assert first["unreachable"] == ""
        cdp = browser.cdp
        failed = {
            "id": "F",
            "url": "chrome-error://chromewebdata/",
            "unreachableUrl": "https://off.example/x",
        }
        cdp.emit("Page.frameNavigated", {"frame": failed}, session="S1")
        while (await screen.until("status"))[0]["unreachable"] != "https://off.example":
            pass
        cdp.answer(
            "Page.getNavigationHistory",
            {"currentIndex": 1, "entries": [{"id": 3, "url": "https://ok.example/"}, {"id": 4, "url": "x"}]},
        )
        await screen.send({"t": "back"})
        await _eventually(lambda: cdp.sent("Page.navigateToHistoryEntry") == [{"entryId": 3}])
    finally:
        await screen.close()


async def test_the_relays_release_lets_go_even_while_an_agents_verb_runs(screen_daemon: ScreenDaemon) -> None:
    """`release` is the relay's, not a person's input: a lease that ended under a held key must not
    leave it down for the agent's resumed verb, so it is never dropped as busy."""
    sock, browser, _, _d = screen_daemon
    screen = await Screen.open(sock)
    try:
        await screen.until("status")
        await screen.send({"t": "key", "type": "down", "key": "Shift", "code": "ShiftLeft", "keyCode": 16})
        await _eventually(lambda: browser.cdp.sent("Input.dispatchKeyEvent"))
        await screen.send({"t": "release"})
        await _eventually(
            lambda: (
                [(k or {}).get("type") for k in browser.cdp.sent("Input.dispatchKeyEvent")]
                == ["rawKeyDown", "keyUp"]
            )
        )
    finally:
        await screen.close()


async def test_a_screen_that_drove_and_closes_lets_go_of_what_it_held(screen_daemon: ScreenDaemon) -> None:
    """The relay's own hop going away (its last viewer left, a restart) can take its `release`
    with it: the guest lets go when a connection that sent input ends."""
    sock, browser, _, _d = screen_daemon
    watcher, driver = await Screen.open(sock), await Screen.open(sock)
    try:
        await watcher.until("status")
        await driver.until("status")
        await driver.send({"t": "pointer", "type": "down", "x": 5, "y": 6, "button": "left", "buttons": 1})
        await _eventually(lambda: browser.cdp.sent("Input.dispatchMouseEvent"))
        await watcher.close()
        await asyncio.sleep(0.05)
        assert len(browser.cdp.sent("Input.dispatchMouseEvent")) == 1, "a watcher holds nothing"
        await driver.close()
        await _eventually(
            lambda: (
                [(m or {}).get("type") for m in browser.cdp.sent("Input.dispatchMouseEvent")]
                == ["mousePressed", "mouseReleased"]
            )
        )
    finally:
        await watcher.close()
        await driver.close()


async def test_a_closed_page_tab_is_dropped_from_the_screen_while_a_verb_holds_the_lock(
    screen_daemon: ScreenDaemon,
) -> None:
    """The page moves off a closed tab only under the verb lock: while an agent's verb holds it (a
    15 s `wait`, seen live) every pane kept the closed tab's last frame. A `reset` drops it at the
    next watch tick; the move, when the lock frees, restarts the screen on the opener."""
    sock, browser, _, d = screen_daemon
    screen = await Screen.open(sock)
    try:
        await screen.until("status")
        cdp = browser.cdp
        cdp.answer("Page.getFrameTree", {"frameTree": {"frame": {"id": "F2"}}})
        info = {"targetId": "T2", "type": "page", "openerId": "T1", "url": "x"}
        cdp.emit(
            "Target.attachedToTarget", {"sessionId": "S2", "targetInfo": info, "waitingForDebugger": True}
        )
        await screen.until("reset")  # the page moved to the tab it opened
        lock = d._lock  # pyright: ignore[reportPrivateUsage]
        await lock.acquire()  # an agent's verb in flight: no move until it ends
        try:
            cdp.dead.add("S2")
            cdp.emit("Target.targetDestroyed", {"targetId": "T2"})
            await screen.until("reset")
        finally:
            lock.release()
    finally:
        await screen.close()


async def test_screen_gets_reset_after_a_chromium_restart_and_the_screencast_resubscribes(
    screen_daemon: ScreenDaemon,
) -> None:
    sock, browser, _, _ = screen_daemon
    screen = await Screen.open(sock)
    try:
        await screen.until("status")
        first = browser.cdp
        await _eventually(lambda: _casts(first) == ["start@60"])
        assert await _verb(sock, {"verb": "die"}) == {"ok": False, "error": "browser restarted"}
        await screen.until("reset")
        assert browser.cdp is not first
        await _eventually(lambda: _casts(browser.cdp) == ["start@60"])  # on the new page
        browser.paint()
        _, jpeg = await screen.until("frame")
        assert jpeg == JPEG
    finally:
        await screen.close()


async def test_screen_bad_message_ends_that_screen_only_and_unknown_types_are_ignored(
    screen_daemon: ScreenDaemon,
) -> None:
    sock, browser, _, _ = screen_daemon
    good, bad, old = await Screen.open(sock), await Screen.open(sock), await Screen.open(sock)
    try:
        for s in (good, bad, old):
            await s.until("status")
        await good.send({"t": "telemetry", "x": 1})  # a newer relay's message: ignored
        await good.send({"t": "key", "type": "down", "key": "a", "code": "KeyA", "text": "a"})
        await _eventually(lambda: browser.cdp.sent("Input.dispatchKeyEvent"))
        bad.writer.write((HEADER_MAX + 1).to_bytes(4, "big") + b"{}")
        await bad.writer.drain()
        bye, _ = await bad.until("bye")
        assert bye["reason"] == "bad_message"
        with pytest.raises(EOFError):
            await bad.recv()
        await old.send({"t": "hello", "v": 2})
        refused, _ = await old.until("bye")
        assert refused["reason"] == "version"
        assert (await _verb(sock, {"verb": "ping"}))["ok"] is True
        mark = len(_casts(browser.cdp))
        await good.send({"t": "hint", "refresh": True})  # the good one is still served
        await _eventually(lambda: _casts(browser.cdp)[mark:] == ["stop", "start@60"])
    finally:
        for s in (good, bad, old):
            await s.close()


async def test_screen_a_reader_that_never_reads_holds_only_its_own_slot_and_bye_is_last(
    screen_daemon: ScreenDaemon,
) -> None:
    """A screen that stops reading blocks only its own writer: the other screen gets every frame,
    newer frames replace the slow one's single unsent frame, and when it is ended its `bye` is
    the last message on its wire - no stale frame follows it."""
    sock, browser, _, _ = screen_daemon
    slow, quick = await Screen.open(sock), await Screen.open(sock)
    try:
        await slow.until("status")
        await quick.until("status")
        big = base64.b64encode(b"\xff\xd8" + b"\0" * (1 << 20)).decode()  # past any socket buffer
        for ack_id in range(1, 5):
            browser.cdp.emit("Page.screencastFrame", {"data": big, "metadata": META, "sessionId": ack_id})
            header, _ = await quick.until("frame")
            assert header["seq"] == ack_id  # never held up by the slow one
        slow.writer.write((HEADER_MAX + 1).to_bytes(4, "big"))
        await slow.writer.drain()
        seen: list[tuple[str, object]] = []
        with contextlib.suppress(EOFError):
            while True:
                header, _ = await slow.recv()
                seen.append((header["t"], header.get("seq", header.get("reason"))))
        # Frame 1 filled its socket; 2 and 3 were replaced unsent; 4 was still waiting at the end.
        assert seen == [("frame", 1), ("bye", "bad_message")]
    finally:
        await slow.close()
        await quick.close()


def test_screen_a_bye_is_the_last_thing_queued_on_a_connection() -> None:
    """The moment a `bye` is queued, the unsent frame goes and nothing more is taken: whatever
    the writer takes next, nothing can follow the bye."""
    conn = _Conn(cast(asyncio.StreamWriter, None))
    conn.ready = True
    conn.post(b"status")
    conn.offer(b"frame-1")
    conn.bye("version")
    conn.offer(b"frame-2")
    conn.post(b"reset")
    assert conn.take() == [b"status", encode({"t": "bye", "reason": "version"})]


async def _read_to_eof(screen: Screen, within: float) -> list[tuple[str, object]]:
    seen: list[tuple[str, object]] = []
    with contextlib.suppress(EOFError):
        while True:
            header, _ = await screen.recv(within=within)
            seen.append((header["t"], header.get("seq", header.get("reason"))))
    return seen


async def test_screen_a_frame_that_lands_with_the_bye_never_follows_it(screen_daemon: ScreenDaemon) -> None:
    """A click repaints the page as a refused `hello` ends the screen, and the departing
    connection's last screencast stop takes a pipe round-trip: its writer wakes then with both
    queued, and still the bye is the last message."""
    sock, browser, _, _ = screen_daemon
    screen = await Screen.open(sock)
    try:
        await screen.until("status")
        await _eventually(lambda: _casts(browser.cdp) == ["start@60"])
        browser.paint_on_pointer, browser.stop_cast_s = True, 0.1
        screen.writer.write(
            encode({"t": "pointer", "type": "move", "x": 1, "y": 1}) + encode({"t": "hello", "v": 2})
        )
        await screen.writer.drain()
        assert await _read_to_eof(screen, within=3.0) == [("bye", "version")]
    finally:
        await screen.close()


def _pumps(hub: object) -> list[asyncio.Task[Any]]:
    """The live writer tasks of one daemon's ScreenHub."""
    out: list[asyncio.Task[Any]] = []
    for task in asyncio.all_tasks():
        coro: Any = task.get_coro()
        frame = getattr(coro, "cr_frame", None)
        if (
            getattr(coro, "__qualname__", "") == "ScreenHub._pump"
            and frame
            and frame.f_locals.get("self") is hub
        ):
            out.append(task)
    return out


async def test_screen_a_departing_screen_is_closed_before_a_hung_screencast_stop(
    screen_daemon: ScreenDaemon,
) -> None:
    """A Chromium that hangs on `Page.stopScreencast` never holds the departing socket open: it
    gets its bye and its EOF at once."""
    sock, browser, _, _ = screen_daemon
    screen = await Screen.open(sock)
    try:
        await screen.until("status")
        await _eventually(lambda: _casts(browser.cdp) == ["start@60"])
        browser.stop_cast_s = 2.0
        screen.writer.write((HEADER_MAX + 1).to_bytes(4, "big"))
        await screen.writer.drain()
        assert await _read_to_eof(screen, within=1.0) == [("bye", "bad_message")]
    finally:
        await screen.close()


async def test_screen_a_failed_screencast_stop_never_leaks_the_departing_writer(
    screen_daemon: ScreenDaemon,
) -> None:
    """The relay closes a screen (no bye), and the last screencast stop fails in a way the hub
    does not expect: the connection's writer task still ends."""
    sock, browser, _, d = screen_daemon
    screen = await Screen.open(sock)
    await screen.until("status")
    await _eventually(lambda: _casts(browser.cdp) == ["start@60"])
    assert len(_pumps(d.screens)) == 1
    browser.stop_cast_error = RuntimeError("not a CDP error")
    await screen.close()
    await _eventually(lambda: not _pumps(d.screens), within=1.0)


async def test_screen_gets_reset_even_when_the_restart_outlasts_a_watch_tick(
    screen_daemon: ScreenDaemon,
) -> None:
    """A relaunch slower than a watch tick: the ticks meanwhile see Chromium down (its `alive` is
    False until the launch ends), and the screen is still told `reset` before the new page's
    frames."""
    sock, browser, _, d = screen_daemon
    screen = await Screen.open(sock)
    try:
        await screen.until("status")
        first = browser.cdp
        await _eventually(lambda: _casts(first) == ["start@60"])
        browser.restart_s = 1.0  # four watch ticks
        starts = d.starts
        assert await _verb(sock, {"verb": "die"}) == {"ok": False, "error": "browser restarted"}
        assert d.starts == starts + 1 and browser.cdp is not first
        await screen.until("reset")
        await _eventually(lambda: _casts(browser.cdp) == ["start@60"])
        browser.paint()
        _, jpeg = await screen.until("frame")
        assert jpeg == JPEG
    finally:
        await screen.close()


async def test_screen_leaving_never_cancels_a_chromium_launch_it_started(screen_daemon: ScreenDaemon) -> None:
    """The last screen leaves while its watch is relaunching a dead Chromium: the launch runs to
    the end (a cancelled one would leave a half-started process the idle stop never reaps)."""
    sock, browser, _, d = screen_daemon
    screen = await Screen.open(sock)
    await screen.until("status")
    browser.restart_s = 1.0
    starts = d.starts
    await browser.stop()  # Chromium died between verbs; the next watch tick relaunches it
    await _eventually(lambda: browser.launching)
    await screen.close()  # the last screen leaves mid-launch
    await _eventually(lambda: not browser.launching and browser.alive)
    assert d.starts == starts + 1


async def test_screen_a_joiner_gets_a_first_frame_on_a_still_page_among_paused_peers(
    screen_daemon: ScreenDaemon,
) -> None:
    """A still page sends nothing on its own, and a frame waiting unacked while every watcher is
    paused holds Chromium back: a new screen is still sent a fresh first frame."""
    sock, browser, _, d = screen_daemon
    browser.paint_on_cast = True  # Chromium's own first frame after `Page.startScreencast`
    a = await Screen.open(sock)
    b: Screen | None = None
    try:
        await a.until("frame")
        await a.send({"t": "hint", "paused": True})
        await _eventually(lambda: d.screens._rate() == 0)  # pyright: ignore[reportPrivateUsage]
        browser.paint()  # a repaint nobody is watching: it waits unacked
        b = await Screen.open(sock)
        await b.until("status")
        _, jpeg = await b.until("frame")
        assert jpeg == JPEG
    finally:
        await a.close()
        if b is not None:
            await b.close()


# ── the bridge ──────────────────────────────────────────────────────────────────────────────────


def test_bridge_says_no_browser_when_no_daemon_listens() -> None:
    """The bridge's side: a warm Machine before its step's first browser verb. The
    bridge never starts the daemon; it says so in one message and exits 0."""
    sock_dir = tempfile.mkdtemp(dir="/tmp", prefix="sc-")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "sidecar.screen"],
            env={**os.environ, "SIDECAR_SOCK": str(Path(sock_dir) / "none.sock"), "PYTHONPATH": str(SIDECAR)},
            input=b"",
            capture_output=True,
            timeout=15,
        )
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)
    assert result.returncode == 0 and result.stderr == b""
    assert result.stdout == encode({"t": "bye", "reason": "no_browser"})
    assert StreamDecoder().feed(result.stdout) == [({"t": "bye", "reason": "no_browser", "n": 0}, b"")]


async def test_bridge_pumps_a_screen_both_ways_and_exits_when_stdin_ends(screen_daemon: ScreenDaemon) -> None:
    sock, browser, _, _ = screen_daemon
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "sidecar.screen",
        env={**os.environ, "SIDECAR_SOCK": sock, "PYTHONPATH": str(SIDECAR)},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None
    bridge = Screen(proc.stdout, proc.stdin)
    try:
        hello, _ = await bridge.recv(within=10.0)
        status, _ = await bridge.recv()
        assert hello["t"] == "hello" and status["t"] == "status" and status["chromium"] == "running"
        await bridge.send({"t": "hello", "v": 1})
        await bridge.send({"t": "pointer", "type": "move", "x": 3, "y": 4})
        await _eventually(lambda: browser.cdp.sent("Input.dispatchMouseEvent"))
        browser.paint()
        _, jpeg = await bridge.until("frame")
        assert jpeg == JPEG
        proc.stdin.close()  # the relay closed the exec: the bridge half-closes and the daemon ends it
        assert await asyncio.wait_for(proc.wait(), 10) == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
