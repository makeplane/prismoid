# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A task's view of the Prismoid Chrome: its CDP relay, and the inner layer
of its browser's egress fence.

`/frames/{ref}/cdp` (`runner/device_door.py`) serves one `TaskRelay` per connection: flattened CDP, as
Chrome's own browser endpoint speaks it, over a WebSocket on the helper's socket - the worker's
`WsCdp` dials it as it dials a remote driver's relay. Every task shares one Chrome and one
profile, so the relay is what keeps a task to its own window. It forwards only the methods listed
below (each with why), only on sessions it opened on its own task's targets; it answers the
browser-wide `Target` calls itself, from its task's targets alone (the target list, discovery,
auto-attach - Chrome-wide state the tasks share); anything else is a CDP error that never reaches
Chrome. A session it opened is the agent's own: the session that fences the page is Chrome's
(`chrome.py`), so no agent can turn its page's fence off.

`RequestFence` is what `Chrome` asks for every request a task's pages, frames and workers make
(`Fetch.requestPaused`): the frame's list, and loopback only for the frame's own ports
(`ports.FramePorts`, looked at afresh before an unknown one is refused). A refusal is recorded in
the frame's `denials` as the proxy records one - host only, never a path or a query string.

One thing is excused (ADR-078): a page on a site already on its task's list may load a
stylesheet, image, font, media file or script from a public host on no list, because a page without
its CDN is bare text. The excuse lifts `Refused.OFF_LIST` and `Refused.NO_LIST` and nothing else,
only for `ASSET_TYPES`, only for a document in the page now and never a worker (`chrome._pages_of`),
only when `pages_on_list` says the top-level page and every document under it are listed (never a
loopback page, which a fenced command can serve, and never about:blank), and reaches the proxy as a
short pass (`on_asset`) that widens no list: no denial, and no allowance.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections import deque
from collections.abc import Callable, Collection, Mapping, Sequence
from enum import StrEnum
from typing import Any, Final, NamedTuple
from urllib.parse import urlsplit

from prismoid.device.chrome import BASE, Chrome, ChromeError, HandedOf, TopOf
from prismoid.device.fence_data import CHROME_BASE
from prismoid.device.frames import DeviceFrameState
from prismoid.device.ports import FramePorts
from prismoid.egress_relay import LOOPBACK_REFUSED, loopback_host, own_literals
from prismoid.schemas.egress import domain_allowed, refuse_ip

LOG = logging.getLogger(__name__)

REFUSED: Final = -32000

# What an agent may send to a session it opened on its task's page: what `BrowserVerbs` and its
# `Recorder` send (`ai/frames/image_browser/sidecar/verbs.py`, `recorder.py`), and nothing more.
SESSION_METHODS: Final[dict[str, str]] = {
    "Accessibility.enable": "the snapshot reads the page's accessibility tree",
    "Accessibility.getFullAXTree": "the snapshot",
    "Accessibility.getPartialAXTree": "an element's live role and name, before it is acted on",
    "DOM.enable": "the verbs address the page's elements",
    "DOM.describeNode": "an element's attributes (a password field is never echoed)",
    "DOM.discardSearchResults": "the end of `wait`'s search",
    "DOM.focus": "typing goes to the element the agent named",
    "DOM.getBoxModel": "where an element is, to click it",
    "DOM.performSearch": "`wait` for an element to appear",
    "DOM.resolveNode": "an element as a script object, to set a value",
    "DOM.scrollIntoViewIfNeeded": "an element is scrolled to before it is clicked",
    "Input.dispatchKeyEvent": "`press` and `type` (never a modified Enter)",
    "Input.dispatchMouseEvent": "`click` and `scroll` (never a modified click, nor the middle button)",
    "Input.insertText": "`type`",
    "Page.enable": "the page's load events settle a verb",
    "Page.bringToFront": "the tab the page moved to is the one shown",
    "Page.captureScreenshot": "`screenshot`, and the recording's frames",
    "Page.getFrameTree": "the page's main frame, and the URL it could not load",
    "Page.getNavigationHistory": "where the page is, and `back`",
    "Page.navigate": "`open` - http(s) and about:blank only, never a local file",
    "Page.navigateToHistoryEntry": "`back`",
    "Page.stopScreencast": "a tab the page left stops painting",
    "Runtime.enable": "the page's execution contexts",
    "Runtime.callFunctionOn": "values set and read on an element",
    "Runtime.evaluate": "page scripts the verbs run (the page's own powers, its requests still vetted)",
    "Runtime.runIfWaitingForDebugger": "a tab the agent was handed starts (it is already running)",
}

# Browser-wide calls (no session): answered from the task's own targets only, by the relay.
BROWSER_METHODS: Final[dict[str, str]] = {
    "Browser.getVersion": "which Chrome this is: a read about no target",
    "Browser.getWindowForTarget": "the window of one of the task's own targets",
    "Target.activateTarget": "bring one of the task's own tabs forward",
    "Target.attachToTarget": "a session of the agent's own on one of the task's targets (flattened only)",
    "Target.closeTarget": "close one of the task's own tabs",
    "Target.createTarget": "a new tab, opened from the task's page so it lands in the task's window",
    "Target.detachFromTarget": "let go of a session this relay opened",
    "Target.getTargetInfo": "one of the task's own targets",
    "Target.getTargets": "the task's own targets - never another task's, nor the person's tabs",
    "Target.setAutoAttach": "the task's new tabs, attached for the agent by the relay - never Chrome-wide",
    "Target.setDiscoverTargets": "the task's own targets' lifecycle events - never Chrome-wide",
}

_NET_SCHEMES: Final = frozenset({"http", "https", "ws", "wss"})
_LOCAL_SCHEMES: Final = frozenset({"data", "blob", "about"})  # no request leaves for these
# `Fetch.requestPaused`'s `resourceType` for what a page needs to render: never a document, an
# iframe, or anything a script reads back.
ASSET_TYPES: Final = frozenset({"Stylesheet", "Image", "Font", "Media", "Script"})


class Refused(StrEnum):
    """What said no. A name, so that what an asset is excused is a set of these and never a reading
    of a sentence: a reason added here is excused by nothing until it joins `ASSET_EXCUSED`."""

    UNREADABLE = "unreadable"
    SCHEME = "scheme"
    NO_HOST = "no_host"
    LOOPBACK = "loopback"
    PRIVATE = "private"
    NO_LIST = "no_list"
    OFF_LIST = "off_list"


ASSET_EXCUSED: Final = frozenset({Refused.NO_LIST, Refused.OFF_LIST})


class Refusal(NamedTuple):
    host: str
    reason: str
    code: Refused


def vet(url: str, allowed: Collection[str], own: Collection[tuple[str, int]]) -> Refusal | None:
    """Why a task's page may not request `url`, or None. Loopback is never on a list: it is only for
    the addresses the task's own commands listen on, each (address, port), never Prismoid's
    own (the proxy holds Chrome to the same, of every live task, and dials no other). The host is
    all a refusal says of the URL."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return Refusal("", "an unreadable URL", Refused.UNREADABLE)
    scheme = parts.scheme.lower()
    if scheme in _LOCAL_SCHEMES:
        return None
    if scheme not in _NET_SCHEMES:
        return Refusal(f"{scheme}:", f"the {scheme} scheme is not for a task's pages", Refused.SCHEME)
    host = (parts.hostname or "").rstrip(".")
    if not host:
        return Refusal("", "no host", Refused.NO_HOST)
    if loopback_host(host):
        if port is None:
            port = 443 if scheme in ("https", "wss") else 80
        return None if own_literals(host, port, own) else Refusal(host, LOOPBACK_REFUSED, Refused.LOOPBACK)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and (why := refuse_ip(literal)) is not None:
        return Refusal(host, why, Refused.PRIVATE)
    if not allowed:
        return Refusal(host, "no allowlist registered for this frame", Refused.NO_LIST)
    if not domain_allowed(host, allowed):
        return Refusal(host, f"not in allowlist {sorted(allowed)}", Refused.OFF_LIST)
    return None


def page_on_list(top: str | None, allowed: Collection[str]) -> bool:
    """Is this committed top-level URL an http(s) site on the list? `vet` passes a local scheme (it
    leaves no machine) and a task's own loopback port is no site: neither is excused by this."""
    if top is None or urlsplit(top).scheme.lower() not in ("http", "https"):
        return False
    return vet(top, allowed, ()) is None


def pages_on_list(pages: Sequence[str], allowed: Collection[str]) -> bool:
    """The top-level page, first, must be on the list. So must every http(s) document under it, on
    the way to the one that asked: a listed page may frame a task's own loopback server. A document
    with no origin of its own (about:blank, srcdoc, blob:) rides on what holds it, so a `srcdoc`
    inside that frame is held by the frame."""
    if not pages or not page_on_list(pages[0], allowed):
        return False
    return all(
        page_on_list(page, allowed)
        for page in pages[1:]
        if urlsplit(page).scheme.lower() in ("http", "https")
    )


def asset_target(url: str, kind: str, refusal: Refusal) -> tuple[str, int] | None:
    """`past_list`, for an asset by `Fetch`'s own word."""
    return past_list(url, refusal) if kind in ASSET_TYPES else None


def past_list(url: str, refusal: Refusal) -> tuple[str, int] | None:
    """The (host, port) a page's request may reach past its list, or None: over http(s), with no
    credential in its URL, and only the list refused it."""
    if refusal.code not in ASSET_EXCUSED:
        return None
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or parts.username is not None or parts.password is not None:
        return None
    return refusal.host, parts.port or (443 if scheme == "https" else 80)


class RequestFence:
    """The inner layer: each request against its task's own list, read at the request (a widened
    list applies at once). `ports` knows each frame's own listening ports (none without it);
    `on_asset` tells the proxy what a request it excused may reach (no excuse without it)."""

    def __init__(
        self,
        frames: Mapping[str, DeviceFrameState],
        ports: FramePorts | None = None,
        base: Collection[str] = CHROME_BASE,
        *,
        on_asset: Callable[[str, int], None] | None = None,
    ) -> None:
        self._frames = frames
        self._ports = ports
        self._base = frozenset(base)
        self._on_asset = on_asset

    async def allows(
        self,
        ref: str | None,
        url: str,
        kind: str = "",
        top: TopOf | None = None,
        handed: HandedOf | None = None,
    ) -> bool:
        frame: DeviceFrameState | None = None
        if ref is None:  # no one task's (a service worker): the proxy's own rule, and no loopback
            allowed: Collection[str] = {host for f in self._frames.values() for host in f.allow}
        elif ref == BASE:  # a page in no task's window: the base list only
            allowed = self._base
        elif (frame := self._frames.get(ref)) is None:
            return False  # the task is gone: its pages reach nothing
        else:
            allowed = frame.allow
        refusal = vet(url, allowed, ())
        if refusal is not None and refusal[1] == LOOPBACK_REFUSED and frame is not None and self._ports:
            # Its own ports as last seen, then afresh: a server just started is not refused.
            for fresh in (False, True):
                own = await self._ports.of(frame, fresh=fresh)
                if (refusal := vet(url, allowed, own)) is None or refusal[1] != LOOPBACK_REFUSED:
                    break
        if refusal is None:
            return True
        # A live task's page only: `frame` is None for a service worker, the base list, a dead task.
        if frame is not None and self._on_asset and (target := past_list(url, refusal)):
            asset = kind in ASSET_TYPES and top is not None and pages_on_list(await top(), frame.allow)
            if asset or (handed is not None and await handed()):
                self._on_asset(*target)
                return True
        self._record(ref, frame, refusal.host, refusal.reason)
        return False

    def listed(self, ref: str, url: str) -> bool:
        return (frame := self._frames.get(ref)) is not None and page_on_list(url, frame.allow)

    def public(self, url: str) -> bool:
        return (refusal := vet(url, (), ())) is not None and refusal.code in ASSET_EXCUSED

    def refused(self, ref: str | None, url: str, reason: str) -> None:
        """Chrome refused this without asking `allows`: kept as a refusal of `allows` is, host only."""
        try:
            host = urlsplit(url).hostname or ""
        except ValueError:
            host = ""
        self._record(ref, self._frames.get(ref or ""), host, reason)

    @staticmethod
    def _record(ref: str | None, frame: DeviceFrameState | None, host: str, reason: str) -> None:
        LOG.warning("DENY %s → %s (%s)", ref or "(chrome)", host, reason)
        if frame is not None:
            frame.denials.append({"ref": frame.ref, "host": host, "reason": reason})


def _same_tab(method: str, params: dict[str, Any]) -> str | None:
    """Why an input event is refused, or None. A click with Cmd, Ctrl, Shift or Alt, a middle
    click, or a modified Enter opens a tab or window with no opener (or a download, or submits a
    form somewhere new): refused, saying so - never rewritten, since a Shift+Enter rewritten to an
    Enter still submits. The task's clicks and keys act in the tab they are sent to; Chrome fences
    a new tab or window anyway, and this is the second line. Other keys keep their modifiers."""
    if method == "Input.dispatchMouseEvent":
        if params.get("button") == "middle":
            return "a middle click opens a tab: click, or open the URL"
        if params.get("type") in ("mousePressed", "mouseReleased") and params.get("modifiers"):
            return "a modified click opens a tab or a window: click without Cmd, Ctrl, Shift or Alt"
    enter = params.get("key") == "Enter" or params.get("code") in ("Enter", "NumpadEnter")
    enter = enter or params.get("windowsVirtualKeyCode") == 13
    if method == "Input.dispatchKeyEvent" and enter and params.get("modifiers"):
        return "a modified Enter opens a tab or a window: press Enter alone"
    return None


def _page_url(url: object) -> str | None:
    """Why `url` may not be opened in a task's tab, or None: http(s), or about:blank."""
    if url == "about:blank":
        return None
    try:
        if isinstance(url, str) and urlsplit(url).scheme.lower() in ("http", "https"):
            return None
    except ValueError:  # `http://[oops`: an agent's typo, refused and not the end of its connection
        pass
    return "a task's tab opens http(s) pages only"


Send = Callable[[dict[str, Any]], None]


# What a parked step's connection says without the agent acting: the recording, kept going.
_NOT_ACTING: Final = frozenset({"Page.screencastFrameAck", "Page.stopScreencast"})


class TaskRelay:
    """One agent connection of one task. `send` queues a message for the agent (in order)."""

    def __init__(self, chrome: Chrome, ref: str, send: Send) -> None:
        self.chrome, self.ref = chrome, ref
        self._send = send
        self.lost = asyncio.Event()
        self.why = ""
        self._sessions: set[str] = set()  # the sessions this relay opened
        self._discover = False
        self._auto_attach = False
        self._jobs: set[asyncio.Task[None]] = set()
        self._behind: deque[dict[str, Any]] = deque()  # commands waiting on a page going back
        chrome.join(self)

    # ── from the agent ───────────────────────────────────────────────────────────────────────

    def handle(self, msg: dict[str, Any]) -> None:
        """One command, answered in order with the events around it. The agent's first ends a
        hand-off, and waits for what that sends back: it must not read a page on its way out."""
        if msg.get("method") not in _NOT_ACTING:
            self.chrome.take_back(self.ref)
        back = self.chrome.going_back(self.ref)
        if back is None and not self._behind:
            self._command(msg)
            return
        self._behind.append(msg)
        if len(self._behind) == 1:
            self._run(self._when_back(back))

    async def _when_back(self, back: asyncio.Event | None) -> None:
        if back is not None:
            await back.wait()
        while self._behind:
            self._command(self._behind.popleft())

    def _command(self, msg: dict[str, Any]) -> None:
        msg_id, method, params, session = (
            msg.get("id"),
            msg.get("method"),
            msg.get("params"),
            msg.get("sessionId"),
        )
        if not isinstance(msg_id, int):
            return
        params = params if params is not None else {}
        if not isinstance(method, str) or not isinstance(params, dict) or not isinstance(session, str | None):
            self._refuse(msg_id, "a command is {id, method, params, sessionId?}")
        elif self.lost.is_set():
            self._refuse(msg_id, self.why)
        elif session is not None:
            self._on_session(msg_id, method, params, session)
        elif method not in BROWSER_METHODS:
            self._refuse(msg_id, f"{method} reaches beyond this task's window")
        else:
            self._on_browser(msg_id, method, params)

    def _on_session(self, msg_id: int, method: str, params: dict[str, Any], session: str) -> None:
        if session not in self._sessions:
            self._refuse(msg_id, "that session is not this task's")
        elif method not in SESSION_METHODS:
            self._refuse(msg_id, f"{method} is not allowed to a task's page")
        elif why := (
            _page_url(params.get("url")) if method == "Page.navigate" else _same_tab(method, params)
        ):
            self._refuse(msg_id, why)
        elif method == "Page.getNavigationHistory":
            self._history(msg_id, session)
        else:
            self._forward(msg_id, method, params, session)

    def _history(self, msg_id: int, session: str) -> None:
        """What a page's history says of a site off the list is its host and path (`Chrome.shown`):
        the rest of what a verb reports of a page comes from here, and it reaches the model."""

        def reply(answer: dict[str, Any]) -> None:
            for entry in (answer.get("result") or {}).get("entries", ()):
                url, title = self.chrome.shown(self.ref, str(entry.get("url", "")))
                if title is not None:
                    entry.update(url=url, userTypedURL=url, title=title)
            self._reply(msg_id, session, answer)

        self.chrome.send("Page.getNavigationHistory", {}, session, reply)

    def _on_browser(self, msg_id: int, method: str, params: dict[str, Any]) -> None:
        match method:
            case "Browser.getVersion":
                self._forward(msg_id, method, {}, None)
            case "Target.getTargets":
                self._answer(msg_id, {"targetInfos": self.chrome.targets_of(self.ref)})
            case "Target.setDiscoverTargets":
                discover = bool(params.get("discover"))
                if discover and not self._discover:
                    for info in self.chrome.targets_of(self.ref):  # as Chrome announces what is open
                        self._send({"method": "Target.targetCreated", "params": {"targetInfo": info}})
                self._discover = discover
                self._answer(msg_id, {})
            case "Target.setAutoAttach":
                if params.get("autoAttach") and params.get("flatten") is not True:
                    self._refuse(msg_id, "only flattened sessions")
                    return
                self._auto_attach = bool(params.get("autoAttach"))
                self._answer(msg_id, {})
            case "Target.createTarget":
                url = params.get("url", "about:blank")
                if why := _page_url(url):
                    self._refuse(msg_id, why)
                    return
                self._run(self._create(msg_id, str(url)))
            case "Target.detachFromTarget":
                session = params.get("sessionId")
                if not isinstance(session, str) or session not in self._sessions:
                    self._refuse(msg_id, "that session is not this task's")
                    return
                self._forward(msg_id, method, {"sessionId": session}, None)
            case _:
                self._on_target(msg_id, method, params)

    def _on_target(self, msg_id: int, method: str, params: dict[str, Any]) -> None:
        """A call that names a target: one of the task's own, or it goes no further."""
        target = params.get("targetId")
        if not isinstance(target, str) or self.chrome.owner_of(target) != self.ref:
            self._refuse(msg_id, f"target {target} is not this task's")
            return
        match method:
            case "Target.attachToTarget":
                if params.get("flatten") is not True:
                    self._refuse(msg_id, "only flattened sessions")
                    return
                self._attach(target, lambda answer: self._reply(msg_id, None, answer))
            case "Target.getTargetInfo":
                self._answer(msg_id, {"targetInfo": self.chrome.info(target)})
            case _:  # activate, close, the window of one of the task's own targets
                self._forward(msg_id, method, {"targetId": target}, None)

    async def _create(self, msg_id: int, url: str) -> None:
        try:
            target = await self.chrome.open_tab(self.ref, url)
        except ChromeError as e:
            self._refuse(msg_id, str(e))
            return
        self._answer(msg_id, {"targetId": target})

    def _attach(self, target: str, then: Callable[[dict[str, Any]], None]) -> None:
        def reply(answer: dict[str, Any]) -> None:
            session = (answer.get("result") or {}).get("sessionId")
            if isinstance(session, str):
                self._sessions.add(session)
                self.chrome.bind(session, self)  # before any event of the session is read
            then(answer)

        self.chrome.send("Target.attachToTarget", {"targetId": target, "flatten": True}, None, reply)

    def _forward(self, msg_id: int, method: str, params: dict[str, Any], session: str | None) -> None:
        self.chrome.send(method, params, session, lambda answer: self._reply(msg_id, session, answer))

    def _reply(self, msg_id: int, session: str | None, answer: dict[str, Any]) -> None:
        out: dict[str, Any] = {"id": msg_id}
        if "error" in answer:
            out["error"] = answer["error"]
        else:
            out["result"] = answer.get("result") or {}
        if session is not None:
            out["sessionId"] = session
        self._send(out)

    def _answer(self, msg_id: int, result: dict[str, Any]) -> None:
        self._send({"id": msg_id, "result": result})

    def _refuse(self, msg_id: int, why: str) -> None:
        self._send({"id": msg_id, "error": {"code": REFUSED, "message": why}})

    def _run(self, work: Any) -> None:
        job = asyncio.get_running_loop().create_task(work)
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)

    # ── from Chrome (`chrome.Relay`) ─────────────────────────────────────────────────────────

    def on_event(self, msg: dict[str, Any]) -> None:
        if msg.get("sessionId") is not None:
            self._send(msg)  # an event on a session the agent opened
        elif msg.get("method") == "Target.detachedFromTarget":
            self._sessions.discard(str((msg.get("params") or {}).get("sessionId")))
            self._send(msg)
        elif self._discover:
            self._send(msg)  # its own target changed, crashed or closed

    def on_joined(self, info: dict[str, Any]) -> None:
        """A tab the task's page opened: told, and attached for the agent if it asked (already
        running - the relay hands over a fenced tab, never a held one)."""
        if self._discover:
            self._send({"method": "Target.targetCreated", "params": {"targetInfo": info}})
        if self._auto_attach and info.get("type") == "page" and not self.lost.is_set():

            def attached(answer: dict[str, Any]) -> None:
                if session := (answer.get("result") or {}).get("sessionId"):
                    self._send(
                        {
                            "method": "Target.attachedToTarget",
                            "params": {"sessionId": session, "targetInfo": info, "waitingForDebugger": False},
                        }
                    )

            self._attach(str(info["targetId"]), attached)

    def on_lost(self, why: str) -> None:
        self.why = why
        self.lost.set()

    def close(self) -> None:
        """The agent went: the sessions it opened are let go; the window and its fence stay."""
        for session in self._sessions:
            self.chrome.send("Target.detachFromTarget", {"sessionId": session}, None, lambda _a: None)
        self._sessions.clear()
        self.chrome.leave(self)
        for job in tuple(self._jobs):
            job.cancel()
