# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# backend/prismoid/ai/frames/image_browser/sidecar/browser.py
"""Chromium, launched and kept: one headless browser over the DevTools pipe, its proxy's credential
answered, its screen streamed and a person's input taken. What the agent does on the
page - the twelve verbs, the snapshot's references, a tab the page opens becoming the page - is
`verbs.BrowserVerbs`, handed this pipe at every start. Nothing here knows
about sockets, tasks or models."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import math
import os
import re
import signal
import subprocess
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import unquote, urlsplit

from .cdp import CdpConnection, PipeTransport
from .verbs import SNAPSHOT_CHARS_DEFAULT, BrowserVerbs

IDLE_STOP_S = 600.0

# The screen. `SCREENCAST_FPS_CAP_DEFAULT = 4` (a session-era guess nothing
# read) is replaced by the amended frame budget: the lease holder sees 30 fps, so
# the source cap is 30 (it was 10 before: a 2-CPU Machine's Chromium paints a scroll at
# 60, and 10 felt laggy to the person driving).
SCREENCAST_QUALITY_DEFAULT = 60
SCREENCAST_FPS_CAP = 30
SCREENCAST_FPS_DEFAULT = 1  # a thumbnail's rate, until a connection asks for more
SCREENCAST_MAX_W, SCREENCAST_MAX_H = 1280, 900  # the native viewport (`--window-size` in `start`)
INPUT_TIMEOUT_S = 5.0  # a page that cannot take an input event in 5 s drops it
MOUSE_TYPES = {"move": "mouseMoved", "down": "mousePressed", "up": "mouseReleased", "wheel": "mouseWheel"}
MOUSE_BUTTONS = frozenset({"none", "left", "middle", "right", "back", "forward"})
WHEEL_MAX = 10_000.0
KEY_FIELD_MAX = 32


CHROMIUM = os.environ.get("SIDECAR_CHROMIUM", "/usr/bin/chromium")
# Every launch flag but the profile. The last block keeps Chromium from calling home on its own -
# component and model downloads, autofill and safe-browsing lookups, pings, Cast discovery,
# translation - none of which changes what a page does, and all of which the egress proxy only
# refuses and the task journal then shows as denials nobody asked for. One `--disable-features`:
# Chromium keeps only the last.
CHROMIUM_FLAGS: tuple[str, ...] = (
    "--headless=new",
    "--remote-debugging-pipe",
    "--no-first-run",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--window-size=1280,900",
    "--disable-sync",
    "--no-default-browser-check",
    "--disable-default-apps",
    "--proxy-bypass-list=127.0.0.1;localhost",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-client-side-phishing-detection",
    "--disable-breakpad",
    "--no-pings",
    "--metrics-recording-only",
    "--disable-features=AutofillServerCommunication,OptimizationHints,OptimizationHintsFetching,"
    "OptimizationGuideModelDownloading,MediaRouter,DialMediaRouteProvider,Translate,"
    "CertificateTransparencyComponentUpdater",
)

# The headed user agent, ONLY when the operator turns it on (`PRISMOID_WORKER_BROWSER_HEADED_UA`, here
# `SIDECAR_HEADED_UA=1`; off by default). Its purpose, said plainly: to get past a site's headless
# filter - an Akamai-fronted site resets the stream (net::ERR_HTTP2_PROTOCOL_ERROR) on headless
# mode's `HeadlessChrome` token alone, and loads under `Chrome` (ADR-066). It is the UA this same Chromium sends with a window - Linux, its major version (UA
# reduction freezes the rest at 0.0.0) - and claims no other browser or OS; nothing else hides
# automation (`navigator.webdriver`, AutomationControlled stay). Off, Chromium's own UA stands and
# such a site ends honestly: "the site turns away automated browsers".
_CHROMIUM_VERSION = re.compile(r"\bChromium (\d+)\.")
_HEADED_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"


def user_agent(version: str) -> str | None:
    """The headed UA for `chromium --version`'s line, or None when it names no version (then
    Chromium's own UA stands)."""
    match = _CHROMIUM_VERSION.search(version)
    return _HEADED_UA.format(major=match.group(1)) if match else None


def launch_flags(version: str, *, headed_ua: bool = False) -> tuple[str, ...]:
    """`CHROMIUM_FLAGS`, plus the headed UA when the operator turned it on and `version` names one."""
    ua = user_agent(version) if headed_ua else None
    return (*CHROMIUM_FLAGS, f"--user-agent={ua}") if ua else CHROMIUM_FLAGS


@functools.cache
def chromium_version() -> str:
    """`chromium --version`, read once per sidecar ("" when it cannot be read)."""
    try:
        done = subprocess.run(
            [CHROMIUM, "--version"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout


def _clamp(value: object, lo: float, hi: float, default: float) -> float:
    """A number off the screen's wire, made safe for CDP: a non-number, or NaN or infinity (which
    `json` parses and Chromium's parser refuses), becomes `default`; the rest is held to [lo, hi]."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return default
    return float(min(hi, max(lo, value)))


def _field(value: object, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


class Browser:
    def __init__(
        self,
        *,
        profile_dir: str,
        proxy: str | None,
        snapshot_chars: int = SNAPSHOT_CHARS_DEFAULT,
        headed_ua: bool = False,
    ) -> None:
        self.profile_dir = profile_dir
        self.headed_ua = headed_ua  # `launch_flags`: the operator's choice, off by default
        self.proxy = proxy
        self._proxy_credential = self._parse_credential()
        # `requestId`s this sidecar has already answered with `ProvideCredentials` - Chromium
        # re-issues `Fetch.authRequired` for the SAME requestId when the credential it was given
        # is wrong/revoked, and answering `ProvideCredentials` again every time loops forever
        # (Puppeteer guards this exact case with its own attempted-authentications set). The
        # second attempt for a requestId always gets `CancelAuth` instead.
        self._auth_attempted: set[str] = set()
        # The daemon's ScreenHub (`on_screencast`); it outlives restarts: `connect` routes every new
        # pipe's frames to it.
        self._screencast_handler: Callable[[dict[str, Any], bytes, int], None] | None = None
        # What a person's screen input holds down, so `release_all` can let go of it when their
        # control ends: a key's `keyUp` params by its code, a button's last position by its name.
        self._held_keys: dict[str, dict[str, Any]] = {}
        self._held_buttons: dict[str, tuple[float, float]] = {}
        self.snapshot_chars = snapshot_chars
        self._proc: subprocess.Popen[bytes] | None = None
        # The page and the verbs on it: a fresh `BrowserVerbs` over each start's pipe (`connect`);
        # before the first start, one over a pipe that answers nothing.
        self._cdp: CdpConnection = PipeTransport()
        self.verbs = BrowserVerbs(
            self._cdp, snapshot_chars=snapshot_chars, on_tab=self._enable_fetch_if_credentialed
        )
        self._wire_launcher(self._cdp)
        # Plain attribute, not a computed property: FakeBrowser (tests) sets `self.alive = True`
        # directly, and a property without a setter would raise on that assignment. start()/stop()
        # set it here; handle() re-checks the real process below so a crashed Chromium is noticed
        # even though `alive` itself is not recomputed on every read.
        self.alive = False

    def connect(self, cdp: CdpConnection) -> BrowserVerbs:
        """Wire a CDP connection: a fresh `BrowserVerbs` over it - its snapshot epochs and page
        moves continuing from the last one's, so a reference minted before a restart never resolves
        after it and an open screen sees every move - and this launcher's own handlers (the proxy's
        challenges, the screencast's frames). `start` calls it with Chromium's pipe; a test, with a
        scripted connection."""
        self._cdp = cdp
        self.verbs = BrowserVerbs(
            cdp,
            snapshot_chars=self.snapshot_chars,
            on_tab=self._enable_fetch_if_credentialed,
            epoch=self.verbs.epoch,
            page_gen=self.verbs.page_gen,
        )
        self._wire_launcher(cdp)
        return self.verbs

    def _wire_launcher(self, cdp: CdpConnection) -> None:
        cdp.on("Fetch.authRequired", self._on_auth_required)
        cdp.on("Fetch.requestPaused", self._on_request_paused)
        cdp.on("Page.screencastFrame", self._on_screencast_frame)

    # ── the page, as the daemon and the screen ask about it (`BrowserVerbs`) ──────────────────

    async def handle(self, cmd: dict[str, Any]) -> dict[str, Any]:
        if self._proc is not None and self._proc.poll() is not None:
            self.alive = False  # the process died under us; note it so the daemon restarts
        return await self.verbs.handle(cmd)

    async def follow(self) -> bool:
        return await self.verbs.follow()

    async def go_back(self) -> None:
        await self.verbs.go_back()

    @property
    def follow_pending(self) -> bool:
        return self.verbs.follow_pending

    @property
    def page_open(self) -> bool:
        return self.verbs.page_open

    @property
    def page_gen(self) -> int:
        return self.verbs.page_gen

    @property
    def origin(self) -> str:
        """The page's origin while Chromium runs (`BrowserVerbs.origin`), "" once it stopped."""
        return self.verbs.origin if self.alive else ""

    @property
    def unreachable(self) -> str:
        """The origin the page could not load while Chromium runs (`BrowserVerbs.unreachable`)."""
        return self.verbs.unreachable if self.alive else ""

    # ── lifecycle ────────────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._proc is not None:
            # A previous start() left a process behind (it crashed mid-handshake, or this is a
            # restart after death): reap it as a unit before spawning a new one, or its profile
            # lock wedges every future start forever.
            await self._kill(self._proc)
            await self._cdp.close()
            self._proc = None
        to_chrome_r, to_chrome_w = os.pipe()  # sidecar -> chromium (fd 3 in the child)
        from_chrome_r, from_chrome_w = os.pipe()  # chromium -> sidecar (fd 4 in the child)
        version = await asyncio.to_thread(chromium_version) if self.headed_ua else ""
        flags = launch_flags(version, headed_ua=self.headed_ua)
        args = [CHROMIUM, *flags, f"--user-data-dir={self.profile_dir}", "about:blank"]
        if self.proxy:
            args.append(self._proxy_arg())
        # Blocking Popen, not asyncio.create_subprocess_exec: this sidecar is stdlib-only and the
        # child's fds must be placed at exactly 3/4 via preexec_fn before exec, which the plain
        # Popen constructor supports directly. close_fds=False (not pass_fds) is required: Popen's
        # own fd-closing step only keeps the ORIGINAL fd numbers from pass_fds open, so it closes
        # the dup2'd 3/4 before exec ever runs. Every other fd this process opens is Python-stdlib
        # and close-on-exec by default, so close_fds=False leaks nothing else. start_new_session
        # gives Chromium its own process group, so a dead/hung browser can be reaped as a unit.
        proc = subprocess.Popen(  # noqa: ASYNC220
            args,
            close_fds=False,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=lambda: (os.dup2(to_chrome_r, 3), os.dup2(from_chrome_w, 4)),
        )
        self._proc = proc
        os.close(to_chrome_r)
        os.close(from_chrome_w)
        try:
            pipe = PipeTransport()
            verbs = self.connect(pipe)
            await pipe.start(read_fd=from_chrome_r, write_fd=to_chrome_w)
            await verbs.attach()
        except Exception:
            # A half-started Chromium is worse than none: kill it now rather than leave an orphan
            # holding the profile lock for the next start() to trip over.
            await self._kill(proc)
            self._proc = None
            raise
        self._auth_attempted = set()  # a requestId cannot outlive the pipe it arrived on
        self._held_keys, self._held_buttons = {}, {}  # a new Chromium holds nothing down
        self.alive = True

    async def stop(self) -> None:
        self.verbs.drop_tabs()  # a stopped browser has no tab to move to
        await self._cdp.close()
        if self._proc is not None:
            await self._kill(self._proc)
        self._proc = None
        self.alive = False

    def _proxy_arg(self) -> str:
        """`--proxy-server=` value for `self.proxy`, with any userinfo stripped. sandbar puts
        `http://user:token@host:port` in `HTTPS_PROXY` (the per-sandbox proxy credential); a
        Chromium flag ends up on this process's argv, readable by anything that can see
        `/proc/<pid>/cmdline` in the sandbox, and Chromium ignores `--proxy-server` userinfo
        anyway (it has no built-in credential support - the credential is answered over the
        separate CDP door, `Fetch.authRequired`, see `_on_auth_required`). So only
        `scheme://hostname:port` (or bare `hostname:port` with no scheme) ever reaches argv."""
        assert self.proxy is not None
        raw = self.proxy
        # `urlsplit` only parses a netloc (and therefore `.hostname`/`.port`) after `//` - a bare
        # `user:pw@host:port` with no scheme is otherwise misread as scheme="user", path=the rest.
        parts = urlsplit(raw if "://" in raw else f"//{raw}")
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        origin = f"{parts.scheme}://{netloc}" if parts.scheme else netloc
        return f"--proxy-server={origin}"

    def _parse_credential(self) -> tuple[str, str] | None:
        """The proxy's userinfo (URL-unquoted), or `None` when `self.proxy` carries none. Never
        touches argv - `_proxy_arg` strips userinfo unconditionally regardless of this result."""
        if not self.proxy:
            return None
        raw = self.proxy
        parts = urlsplit(raw if "://" in raw else f"//{raw}")
        if parts.username is None:
            return None
        return (unquote(parts.username), unquote(parts.password or ""))

    async def _enable_fetch_if_credentialed(self, session: str | None) -> None:
        """`Fetch.enable` with `handleAuthRequests` is only ever turned on when there is a proxy
        credential to answer with - without one, every `Fetch.requestPaused` would still need
        `Fetch.continueRequest` or the page hangs, for no benefit at all. Per tab: `session`."""
        if self._proxy_credential:
            await self.verbs.call_on(
                session, "Fetch.enable", {"handleAuthRequests": True, "patterns": [{"urlPattern": "*"}]}
            )

    def _on_auth_required(self, msg: dict[str, Any]) -> None:
        """A `Server`-sourced challenge (a site's own 401) is cancelled, never answered with the
        proxy credential - only a `Proxy`-sourced challenge, matching sandbar's own tunnel, gets
        it. The credential never appears in a log or a result here.

        A wrong/revoked credential makes Chromium re-issue `Fetch.authRequired` for the SAME
        `requestId` rather than fail the request - answering `ProvideCredentials` again every
        time loops forever. `_auth_attempted` tracks requestIds already given the credential
        once; a second attempt for the same id gets `CancelAuth` instead, so a bad credential
        fails the request exactly once."""
        params = msg.get("params", {})
        challenge = params.get("authChallenge", {})
        request_id = params.get("requestId")
        if (
            challenge.get("source") == "Proxy"
            and self._proxy_credential
            and request_id not in self._auth_attempted
        ):
            self._auth_attempted.add(request_id)
            user, password = self._proxy_credential
            response: dict[str, Any] = {
                "response": "ProvideCredentials",
                "username": user,
                "password": password,
            }
        else:
            response = {"response": "CancelAuth"}
        self.verbs.fire(
            "Fetch.continueWithAuth",
            {"requestId": request_id, "authChallengeResponse": response},
            session=msg.get("sessionId"),  # the tab that asked, in the background or not
        )

    def _on_request_paused(self, msg: dict[str, Any]) -> None:
        """`Fetch.enable` pauses every matching request, not just the auth handshake - each one
        must be continued or the page hangs forever. This is also the cheapest hook for a
        request's `requestId` to have finished with this sidecar, so it doubles as the point
        `_auth_attempted` forgets that id - nothing else ever revisits it once continued."""
        request_id = msg.get("params", {}).get("requestId")
        self._auth_attempted.discard(request_id)
        self.verbs.fire("Fetch.continueRequest", {"requestId": request_id}, session=msg.get("sessionId"))

    async def _kill(self, proc: subprocess.Popen[bytes]) -> None:
        """Terminate a Chromium process group, escalating to SIGKILL if it won't go quietly."""
        if proc.poll() is not None:
            return
        loop = asyncio.get_running_loop()
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(loop.run_in_executor(None, proc.wait), timeout=5.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            await loop.run_in_executor(None, proc.wait)

    # ── the screen: one screencast down, a person's input up ──────────────────────────────────

    def running(self) -> bool:
        """`alive`, re-checked against the process, so a Chromium that died between verbs is
        noticed by an open screen as well as by the next verb (`handle` makes the same check)."""
        if self._proc is not None and self._proc.poll() is not None:
            self.alive = False
        return self.alive

    def on_screencast(self, handler: Callable[[dict[str, Any], bytes, int], None]) -> None:
        """`handler(metadata, jpeg, frame_ack_id)` for every `Page.screencastFrame`. One handler
        (the daemon's ScreenHub), registered once; it survives restarts."""
        self._screencast_handler = handler

    def _on_screencast_frame(self, msg: dict[str, Any]) -> None:
        if not self.verbs.here(msg):
            return  # a tab in the background: its screencast was stopped when the page moved
        params = msg.get("params", {})
        try:
            frame_ack_id = int(params["sessionId"])  # CDP's name for the frame's ack id
        except (KeyError, TypeError, ValueError):
            return
        try:
            jpeg = base64.b64decode(params.get("data", ""), validate=True)
        except (TypeError, ValueError):  # binascii.Error is a ValueError
            jpeg = b""  # still handed on, empty, so it is acked and the screencast never stalls
        if self._screencast_handler is None:
            self.ack_frame(frame_ack_id)  # nobody is watching: never leave Chromium waiting
            return
        self._screencast_handler(dict(params.get("metadata") or {}), jpeg, frame_ack_id)

    async def start_screencast(self, *, quality: int) -> None:
        await self.verbs.call(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": quality,
                "maxWidth": SCREENCAST_MAX_W,
                "maxHeight": SCREENCAST_MAX_H,
                "everyNthFrame": 1,
            },
        )

    async def stop_screencast(self) -> None:
        await self.verbs.call("Page.stopScreencast")

    def ack_frame(self, frame_ack_id: int) -> None:
        """Chromium sends no new frame until the last is acked: the ack IS the frame rate."""
        self.verbs.fire("Page.screencastFrameAck", {"sessionId": frame_ack_id})

    async def pointer(self, ev: Mapping[str, Any]) -> None:
        """`{type: move|down|up|wheel, x, y, button, buttons, clicks, modifiers, dx, dy}`, in
        viewport CSS pixels, onto `Input.dispatchMouseEvent`. Any other type is ignored, and every
        number is clamped (`_clamp`)."""
        kind = MOUSE_TYPES.get(str(ev.get("type")))
        if kind is None:
            return
        button = ev.get("button")
        params: dict[str, Any] = {
            "type": kind,
            "x": _clamp(ev.get("x"), 0, SCREENCAST_MAX_W, 0),
            "y": _clamp(ev.get("y"), 0, SCREENCAST_MAX_H, 0),
            "button": button if isinstance(button, str) and button in MOUSE_BUTTONS else "none",
            "buttons": int(_clamp(ev.get("buttons"), 0, 31, 0)),
            "modifiers": int(_clamp(ev.get("modifiers"), 0, 15, 0)),
        }
        if kind in ("mousePressed", "mouseReleased"):
            params["clickCount"] = int(_clamp(ev.get("clicks"), 1, 3, 1))
        elif kind == "mouseWheel":
            params["deltaX"] = _clamp(ev.get("dx"), -WHEEL_MAX, WHEEL_MAX, 0)
            params["deltaY"] = _clamp(ev.get("dy"), -WHEEL_MAX, WHEEL_MAX, 0)
        at = (params["x"], params["y"])
        if kind == "mousePressed" and params["button"] != "none":
            self._held_buttons[params["button"]] = at
        elif kind == "mouseReleased":
            self._held_buttons.pop(params["button"], None)
        elif kind == "mouseMoved":
            self._held_buttons = dict.fromkeys(self._held_buttons, at)  # a drag carries what it holds
        await self._input("Input.dispatchMouseEvent", params)

    async def key(self, ev: Mapping[str, Any]) -> None:
        """`{type: down|up, key, code, text, modifiers, keyCode}` onto `Input.dispatchKeyEvent`.
        A key-down that types (`text`) is `keyDown`; one that does not (Backspace, an arrow) is
        `rawKeyDown`, Puppeteer's split, so it acts once. Enter carries a carriage return, as
        `BrowserVerbs._key` does: Blink submits a form on the keypress, which needs it."""
        kind = ev.get("type")
        if kind not in ("down", "up"):
            return
        key, code = _field(ev.get("key"), KEY_FIELD_MAX), _field(ev.get("code"), KEY_FIELD_MAX)
        text = _field(ev.get("text"), 8)
        vk = int(_clamp(ev.get("keyCode"), 0, 255, 0))
        if key == "Enter":
            text, vk = text or "\r", vk or 13
        params: dict[str, Any] = {
            "key": key,
            "code": code,
            "modifiers": int(_clamp(ev.get("modifiers"), 0, 15, 0)),
        }
        if vk:
            params["windowsVirtualKeyCode"] = vk
        held = code or key
        if kind == "up":
            params["type"] = "keyUp"
            self._held_keys.pop(held, None)
        else:
            self._held_keys[held] = {**params, "type": "keyUp"}
            if text:
                params |= {"type": "keyDown", "text": text, "unmodifiedText": text}
            else:
                params["type"] = "rawKeyDown"
        await self._input("Input.dispatchKeyEvent", params)

    async def release_all(self) -> None:
        """Let go of every key and button a person's input holds down: their
        control ended, maybe where their page never heard it (an owner's End, an expiry, a re-claim
        from another tab, a tab gone), and the page must not be left mid-drag or with Shift down for
        whoever acts next. Keys first, then buttons, each at its last position."""
        keys, buttons = self._held_keys, self._held_buttons
        self._held_keys, self._held_buttons = {}, {}
        for params in keys.values():
            await self._input("Input.dispatchKeyEvent", params)
        for button, (x, y) in buttons.items():
            params = {"type": "mouseReleased", "x": x, "y": y, "button": button, "buttons": 0, "modifiers": 0}
            await self._input("Input.dispatchMouseEvent", {**params, "clickCount": 1})

    async def insert_text(self, text: str) -> None:
        """A paste: typed as text, never through the clipboard (nothing is ever copied out)."""
        await self._input("Input.insertText", {"text": text})

    async def _input(self, method: str, params: dict[str, Any]) -> None:
        await self._cdp.call(method, params, session_id=self.verbs.session, timeout=INPUT_TIMEOUT_S)
