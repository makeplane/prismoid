# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# backend/prismoid/ai/frames/image_browser/sidecar/verbs.py
"""The browser verbs: one page at a time (a tab
the page opens becomes the page), twelve verbs, the snapshot's references (`tree.py`) and the
egress refusal's wording, over any CDP connection (`cdp.CdpConnection`) - the daemon's pipe to the
Chromium it launched (`browser.Browser`), or a worker's connection to a computer's browser.
Nothing here launches a browser, streams a screen or takes a person's input. Internal
`Runtime.callFunctionOn` is used to set form values and is never exposed as a verb (the spec bans
model-facing JavaScript evaluation)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any, Final, cast
from urllib.parse import urlsplit

from . import watch
from .cdp import CdpClosed, CdpConnection, CdpError
from .scripts import INSPECT_JS, WHOLE_VALUE_FORMATS, WHOLE_VALUE_JS
from .tree import Screen, interactive_ids, proof_read, render, screen_of

SNAPSHOT_CHARS_DEFAULT = 12_000
SNAPSHOT_CHARS_MAX = 400_000  # a system read (the proof check's) and a page saved to a file
COMMAND_TIMEOUT_S = 30.0
WAIT_S = 10.0  # the most a `wait` waits for its text

FOLLOW_TIMEOUT_S = 5.0  # each call of a move to another tab: a tab this slow keeps the lock no longer
FOLLOW_TRIES = 3  # a tab that fails three moves is left where it is, while the page's own tab is open
FOLLOW_HOPS = 4  # one move steps past at most this many tabs that close while it is under way

ACTING_VERBS = frozenset({"open", "back", "click", "type", "fill", "select", "press", "scroll"})
VERBS = ACTING_VERBS | {"wait", "snapshot", "screenshot", "inspect"}

OPEN_SCHEMES = frozenset({"http", "https"})

# Chromium attaches every page opened after the start itself, held before its first request until
# `Runtime.runIfWaitingForDebugger` (`BrowserVerbs._adopt`); pages only - no worker or tab target.
AUTO_ATTACH: Final[dict[str, Any]] = {
    "autoAttach": True,
    "waitForDebuggerOnStart": True,
    "flatten": True,
    "filter": [{"type": "page"}, {"exclude": True}],
}


def _center_of(quad: list[float]) -> tuple[float, float]:
    """The click point: the mean of the content quad's four corners (unchanged since v1)."""
    xs, ys = quad[0::2], quad[1::2]
    return (sum(xs) / 4, sum(ys) / 4)


def _box_of(quad: list[float]) -> dict[str, float]:
    """The quad's bounding box in viewport CSS px, for `screen.action`. The screencast
    is the same 1280x900 viewport, so a watcher's pane draws it exactly where the agent acted."""
    xs, ys = quad[0::2], quad[1::2]
    return {"x": min(xs), "y": min(ys), "w": max(xs) - min(xs), "h": max(ys) - min(ys)}


def _origin_of(url: str) -> str:
    """`scheme://host[:port]` of a web page's URL - never its path, query or userinfo; "" for a
    page that is not on the web (about:blank, data:, an error page)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return ""
    host = parts.hostname or ""
    if parts.scheme not in OPEN_SCHEMES or not host:
        return ""
    host = f"[{host}]" if ":" in host else host  # an IPv6 literal keeps its brackets
    return f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")


def _unreachable_url(tree: dict[str, Any]) -> str:
    """The URL a frame tree's main frame could not load, "" when it loaded."""
    url = tree.get("frameTree", {}).get("frame", {}).get("unreachableUrl")
    return url if isinstance(url, str) else ""


class StaleReference(Exception):
    def __init__(self, ref: str) -> None:
        super().__init__(ref)
        self.ref = ref


TabSetup = Callable[[str], Awaitable[None]]
"""Sets a tab up while Chromium holds it at its start - the first page, and every page it opens -
before the tab may run: the daemon answers its proxy's credential there
(`Browser._enable_fetch_if_credentialed`)."""


class BrowserVerbs:
    """One browser's page and the twelve verbs on it, over a CDP connection. The page is ONE tab, the
    one verbs and a screen act on (`_target`, `_session`); a tab it opens is attached by Chromium
    itself, held at its start until it is set up (`_adopt`), and becomes the page at the next
    `follow()`, under the owner's verb lock. Every tab stays attached, so a page in the background
    still has its proxy challenges answered. Events only record what happened; where the page
    belongs is decided at the move (`_desired`), so no order of opens and closes can leave it on a
    closed tab. A refused call (`CdpError`) and a stale reference are a verb's results; anything
    else a verb meets propagates to its owner: a dead connection (`CdpClosed`, `ConnectionError`),
    which the owner answers by reconnecting, and any other exception (a `TimeoutError`, a reply
    missing a field), which the owner turns into a result (`Daemon.dispatch` does both)."""

    def __init__(
        self,
        cdp: CdpConnection,
        *,
        snapshot_chars: int = SNAPSHOT_CHARS_DEFAULT,
        on_tab: TabSetup | None = None,
        epoch: int = 0,
        page_gen: int = 0,
    ) -> None:
        self.cdp = cdp
        self.snapshot_chars = snapshot_chars
        self._on_tab = on_tab
        self._inflight: set[asyncio.Task[None]] = set()
        self._session: str | None = None
        self._target: str | None = None
        self._tabs: dict[str, str] = {}  # target id -> session id, every attached tab
        self._order: list[str] = []  # the tabs in the order they opened, the first page first
        self._openers: dict[str, str] = {}  # a tab -> the tab that opened it (or was the page)
        self._adopting: dict[str, asyncio.Task[None]] = {}
        self._failed: dict[str, int] = {}  # a tab -> the moves to it that failed
        self._follow_to: str | None = None  # the newest tab opened since the page last moved
        self._urls: dict[str, str] = {}  # target id -> its URL, as Chromium last reported it
        # session id -> the URL its main frame could not load ("" once one loaded): Chromium shows
        # its own error page then, which a refused proxy tunnel lands on (`unreachable`)
        self._unreachable: dict[str, str] = {}
        self._discovering = False  # the tabs `Target.setDiscoverTargets` reports as already open
        # Every move of the page to another tab: an open screen restarts on it. It continues across
        # connections (`Browser.connect`), as the epoch does.
        self.page_gen = page_gen
        self._acted = self.unsettled = False  # the verb's act reached the page (`acts`); its settle ran out
        # Snapshot epochs continue across connections: a reference minted before a restart never
        # resolves against the next page's elements.
        self._epoch = epoch
        self._refs: dict[str, int] = {}
        self._meta: dict[str, tuple[str, str]] = {}
        self._seen: set[int] | None = None  # the controls of the last look at this document (`*`)
        self.requests = watch.Requests(lambda: self._session)  # the page tab's, for the settle
        self._loaded = asyncio.Event()
        # The settle state machine (`_settle`): a verb that does not navigate (a `preventDefault()`'d
        # submit, a bfcache `back`) fires no `Page.loadEventFired` at all. These track whether the
        # main frame started a real navigation since the act's `_arm()`.
        self._navigating = False
        self._nav_started = asyncio.Event()
        self._main_frame_id: str | None = None
        self._wire_events()

    @property
    def epoch(self) -> int:
        """The last snapshot's epoch: the next connection continues from it (`Browser.connect`)."""
        return self._epoch

    @property
    def session(self) -> str | None:
        """The page's tab's CDP session: where a screen's frames come from and a person's input goes."""
        return self._session

    def _wire_events(self) -> None:
        """Registered before the connection carries anything, so a test drives the same handlers
        over a scripted connection with no Chromium at all."""
        self.cdp.on("Page.frameStartedLoading", self._on_frame_started_loading)
        self.cdp.on("Page.loadEventFired", self._on_load_event_fired)
        self.cdp.on("Page.frameStoppedLoading", self._on_frame_stopped_loading)
        self.cdp.on("Page.frameNavigated", self._on_frame_navigated)
        self.cdp.on("Target.targetCreated", self._on_target_info_changed)  # its URL; the attach adopts
        self.cdp.on("Target.attachedToTarget", self._on_attached)
        self.cdp.on("Target.targetInfoChanged", self._on_target_info_changed)
        self.cdp.on("Target.targetDestroyed", self._on_target_destroyed)
        self.cdp.on("Target.detachedFromTarget", self._on_detached)  # a closing tab's session
        for event in ("Network.requestWillBeSent", "Network.loadingFinished", "Network.loadingFailed"):
            self.cdp.on(event, self.requests.seen)

    async def attach(self) -> None:
        """Take the browser's first page as the page and set it up, then have Chromium attach every
        page opened from now on, held at its start until it is set up (`_adopt`)."""
        page = await self._wait_for_page_target()
        att = await self.cdp.call("Target.attachToTarget", {"targetId": page["targetId"], "flatten": True})
        target, session = str(page["targetId"]), str(att["sessionId"])
        self._target, self._session = target, session
        self._tabs, self._order, self._openers = {target: session}, [target], {}
        self._urls = {target: str(page.get("url") or "")}
        self._failed, self._follow_to = {}, None
        for domain in ("Page", "DOM", "Accessibility", "Runtime"):
            await self.call(f"{domain}.enable")
        with contextlib.suppress(CdpError):  # without it, the settle reads the page's quiet alone
            await self.call("Network.enable", watch.NETWORK_ENABLE)
        if self._on_tab is not None:
            await self._on_tab(session)
        tree = await self.call("Page.getFrameTree")
        self._main_frame_id = tree["frameTree"]["frame"]["id"]
        self._unreachable = {session: _unreachable_url(tree)}
        self._discovering = True  # what it reports while answering was open before: never followed
        try:
            await self.cdp.call("Target.setDiscoverTargets", {"discover": True})
            # Every page opened from now on is attached by Chromium and held at its start until
            # `_adopt` has set it up: its first request cannot reach a
            # credentialed proxy before the tab's auth handler is there.
            await self.cdp.call("Target.setAutoAttach", AUTO_ATTACH)
        finally:
            self._discovering = False

    def drop_tabs(self) -> None:
        """The browser stopped: no tab is left to move to."""
        for task in self._adopting.values():
            task.cancel()
        self._tabs, self._order, self._openers, self._adopting = {}, [], {}, {}
        self._failed, self._follow_to, self._urls = {}, None, {}

    @property
    def origin(self) -> str:
        """The page's origin - `scheme://host[:port]` of its tab's URL as Chromium reports it, ""
        for no web page. The screen's address line: a fact this sidecar states, so the person
        deciding whether to type into the page never has only the agent's word for where it is."""
        if self._target is None:
            return ""
        return _origin_of(self._urls.get(self._target, ""))

    @property
    def unreachable(self) -> str:
        """The origin the page's tab could not load - it shows Chromium's error page for it - ""
        when its page loaded. What the screen says beside the address line (a refused proxy tunnel:
        a host outside the frame's egress allowlist, or one that did not answer), and why a person
        is offered the way back (`go_back`)."""
        if self._session is None:
            return ""
        return _origin_of(self._unreachable.get(self._session, ""))

    async def _adopt(self, target: str, session: str) -> None:
        """Set a tab up while Chromium holds it at its start - its proxy answers on - and only
        then let it run: so its first request never reaches a credentialed proxy before the
        tab's auth handler exists (before, the first request of a fresh
        Chromium in a new tab failed on the 407). The page reaches it only at the next `follow()`."""
        self._tabs[target] = session
        try:
            if self._on_tab is not None:
                with contextlib.suppress(CdpError, CdpClosed, ConnectionError, TimeoutError):
                    await self._on_tab(session)
        finally:
            with contextlib.suppress(CdpError, CdpClosed, ConnectionError, TimeoutError):
                await self.call_on(session, "Runtime.runIfWaitingForDebugger")

    def here(self, msg: dict[str, Any]) -> bool:
        """An event of the page's own tab, or of no tab: a tab in the background never settles a
        verb or paints the screen."""
        session = msg.get("sessionId")
        return session is None or session == self._session

    def _on_frame_started_loading(self, msg: dict[str, Any]) -> None:
        if self.here(msg) and msg.get("params", {}).get("frameId") == self._main_frame_id:
            self._navigating = True
            self._nav_started.set()

    def _on_load_event_fired(self, msg: dict[str, Any]) -> None:
        if self.here(msg):
            self._on_settled()

    def _on_frame_stopped_loading(self, msg: dict[str, Any]) -> None:
        if self.here(msg) and msg.get("params", {}).get("frameId") == self._main_frame_id:
            self._on_settled()

    def _on_frame_navigated(self, msg: dict[str, Any]) -> None:
        """A tab's main frame committed: a page, or Chromium's own error page for a URL it could
        not load (`unreachableUrl`). Only the page's tab has `Page` events on; a tab the page moves
        to is read at the move (`_move`)."""
        frame = msg.get("params", {}).get("frame", {})
        session = msg.get("sessionId") or self._session
        if session is None or frame.get("parentId"):
            return
        url = frame.get("unreachableUrl")
        self._unreachable[session] = url if isinstance(url, str) else ""
        if session == self._session:
            self._seen = None  # a new document: nothing on it is "new since the last look"

    def _on_settled(self) -> None:
        self._navigating = False
        self._loaded.set()

    # ── tabs: a tab the page opens becomes the page ──────────────────────────────────────────

    def _on_attached(self, msg: dict[str, Any]) -> None:
        """A page Chromium attached on its own (`AUTO_ATTACH`): a tab the page opened (a
        `target=_blank` link, `window.open`, a person's Ctrl-click), held at its start. It is set
        up at once (`_adopt`), and the page moves to it at the next `follow()`. A `target=_blank`
        link is `noopener` by default, so a tab that names no opener we know came from the page. A
        tab a tab in the background opened (a late popup, an ad) is adopted too - its proxy
        answered - but never takes the page from where the flow is. Anything else is let go at
        once: a page open before the browser started, one already ours, and a page with a subtype
        (a prerender a site's speculation rules asked for, nobody's tab)."""
        if msg.get("sessionId") is not None:
            return  # only the browser's own attaches: no tab's session auto-attaches anything
        params = msg.get("params", {})
        info = params.get("targetInfo", {})
        target, session = info.get("targetId"), params.get("sessionId")
        if not isinstance(target, str) or not isinstance(session, str):
            return
        self._on_target_info_changed(msg)
        if (
            self._discovering
            or info.get("type") != "page"
            or info.get("subtype")
            or target in self._tabs
            or target in self._adopting
            or target in self._openers
        ):
            self._let_go(session)
            return
        opener = info.get("openerId")
        known = isinstance(opener, str) and self._open(opener)
        self._openers[target] = opener if known else (self._target or "")
        self._order.append(target)
        if not known or opener in (self._target, self._follow_to):  # the page, or where it is going
            self._follow_to = target
        task = asyncio.get_running_loop().create_task(self._adopt(target, session))
        self._adopting[target] = task
        task.add_done_callback(lambda _t: self._adopting.pop(target, None))

    def _on_target_info_changed(self, msg: dict[str, Any]) -> None:
        """A tab's URL as Chromium reports it (at its creation, its attach, and every navigation
        it commits): what `origin` reads for the page's tab."""
        info = msg.get("params", {}).get("targetInfo", {})
        target, url = info.get("targetId"), info.get("url")
        if info.get("type") == "page" and isinstance(target, str) and isinstance(url, str):
            self._urls[target] = url

    def _let_go(self, session: str) -> None:
        """A page attached that is not a tab of ours: let it run, and drop the session."""

        async def go() -> None:
            with contextlib.suppress(CdpError, CdpClosed, ConnectionError, TimeoutError):
                await self.call_on(session, "Runtime.runIfWaitingForDebugger")
                await self.cdp.call("Target.detachFromTarget", {"sessionId": session})

        task = asyncio.get_running_loop().create_task(go())
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    def _on_target_destroyed(self, msg: dict[str, Any]) -> None:
        """A tab closed: its target destroyed."""
        target = msg.get("params", {}).get("targetId")
        if isinstance(target, str):
            self._tab_gone(target)

    def _on_detached(self, msg: dict[str, Any]) -> None:
        """A session detached. Known by its session (a detach's `targetId` is deprecated and may
        be absent), and only a tab's own session closes it: a page we let go detaches too."""
        session = msg.get("params", {}).get("sessionId")
        target = next((t for t, s in self._tabs.items() if s == session), None)
        if target is not None:
            self._tab_gone(target)

    def _tab_gone(self, target: str) -> None:
        """Only the fact is kept: where the page goes is `_desired`'s to decide."""
        if (task := self._adopting.pop(target, None)) is not None:
            task.cancel()
        if (session := self._tabs.pop(target, None)) is not None:
            self._unreachable.pop(session, None)
        self._urls.pop(target, None)
        if target == self._target:  # a verb waiting for the page's load waits no more
            self._loaded.set()
            self._nav_started.set()

    def _open(self, target: str) -> bool:
        """Still open: attached, or its attach is on the wire."""
        return target in self._tabs or target in self._adopting

    def _movable(self, target: str) -> bool:
        return self._open(target) and self._failed.get(target, 0) < FOLLOW_TRIES

    def _desired(self) -> str | None:
        """Where the page belongs: the newest tab it opened since it last moved, while that tab
        is open; else where it is, while that is open; else the tab that opened it, or that one's
        opener, and so on; else the newest tab still open. None: no tab is left. Once the page's
        own tab is gone, a tab given up on as too slow still beats none: left on a closed tab, the
        page refuses every verb until Chromium restarts, and a slow tab may yet answer."""
        if self._follow_to is not None and self._movable(self._follow_to):
            return self._follow_to
        if self._target is not None and self._open(self._target):
            return self._target
        back: list[str] = []
        opener = self._openers.get(self._target or "")
        while opener and opener not in back:
            back.append(opener)
            opener = self._openers.get(opener)
        back += reversed(self._order)
        return next((t for t in back if self._movable(t)), None) or next(
            (t for t in back if self._open(t)), None
        )

    @property
    def page_open(self) -> bool:
        """The page's own tab is still open. False from its close until the next move takes the
        page elsewhere (`follow`, under the verb lock)."""
        return self._target is not None and self._open(self._target)

    @property
    def follow_pending(self) -> bool:
        desired = self._desired()
        return desired is not None and desired != self._target

    async def follow(self) -> bool:
        """Move the page to where it belongs (`_desired`). Only under the daemon's verb lock
        (`Daemon.dispatch`, `Daemon.follow_tab`, the end of an acting verb), so no verb runs across
        a move. A tab that closes during the move is stepped past in the same move, so a verb never
        starts on a closed tab; one that answers too slowly is tried again at the next move,
        `FOLLOW_TRIES` times, and then the page is brought back to the front (while the page's own
        tab is open - with it closed, `_desired` keeps the slow tab). True when the page moved."""
        for _ in range(FOLLOW_HOPS):
            target = self._desired()
            if target is None or target == self._target:
                return False
            moved = await self._move(target)
            if moved is not None:
                return moved
        return False

    async def _move(self, target: str) -> bool | None:
        """One move to `target`: True moved, False failed (tried again later), None it closed. The
        new page is brought to the front: a tab in the background paints nothing, so the screen
        froze on it. Every reference of the old page goes stale, and an open screen
        restarts on the new one (`page_gen`)."""
        if (task := self._adopting.get(target)) is not None:
            await asyncio.wait({task})  # its attach, done or cancelled; never our own cancellation
        session = self._tabs.get(target)
        if session is None:
            return None  # it closed, or its attach failed
        try:
            for domain in ("Page", "DOM", "Accessibility", "Runtime"):
                await self.call_on(session, f"{domain}.enable", timeout=FOLLOW_TIMEOUT_S)
            with contextlib.suppress(CdpError):
                await self.call_on(session, "Network.enable", watch.NETWORK_ENABLE, timeout=FOLLOW_TIMEOUT_S)
            tree = await self.call_on(session, "Page.getFrameTree", timeout=FOLLOW_TIMEOUT_S)
            main_frame = tree["frameTree"]["frame"]["id"]
            await self.call_on(session, "Page.bringToFront", timeout=FOLLOW_TIMEOUT_S)
        except (CdpError, TimeoutError, KeyError):
            if self._tabs.get(target) != session:
                return None  # it closed under the move (its calls fail at its detach)
            self._failed[target] = self._failed.get(target, 0) + 1
            if self._failed[target] >= FOLLOW_TRIES and self._session is not None:
                self.fire("Page.bringToFront", {})  # given up: the page verbs act on is the one shown
            return False
        if self._tabs.get(target) != session:
            return None  # it closed while it was brought forward
        self._unreachable[session] = _unreachable_url(tree)  # no `Page` events reached it before
        old = self._session
        self._target, self._session, self._main_frame_id = target, session, main_frame
        self._refs, self._meta, self._seen = {}, {}, None
        self.requests.clear()
        self._failed.pop(target, None)  # it answered: a later slow spell starts its tries afresh
        if self._follow_to == target:
            self._follow_to = None  # a tab opened during the move stays pending
        self.page_gen += 1
        if old is not None:
            self.fire("Page.stopScreencast", {}, session=old)  # the old tab's screencast ends here
        return True

    def fire(self, method: str, params: dict[str, Any], *, session: str | None = None) -> None:
        """Fire-and-forget a CDP call from inside the read loop's synchronous event dispatch,
        which must never await inline. Errors are suppressed: a detached target or a closed pipe
        racing this response is not this sidecar's problem to raise from an event handler. The
        task is kept in `self._inflight` (discarded on completion) so it is not dropped mid-flight
        - a bare `create_task()` result with no reference held is eligible for GC before it runs.
        It goes to the tab `session`, or to the page's when none is named."""
        target = self._session if session is None else session

        async def go() -> None:
            with contextlib.suppress(CdpError, CdpClosed, ConnectionError, TimeoutError):
                await self.call_on(target, method, params)

        task = asyncio.get_running_loop().create_task(go())
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _wait_for_page_target(self) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + 5.0
        while True:
            targets = (await self.cdp.call("Target.getTargets"))["targetInfos"]
            for t in targets:
                if t["type"] == "page":
                    return t
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError("no page target appeared within 5s")
            await asyncio.sleep(0.1)

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, acts: bool = False
    ) -> dict[str, Any]:
        return await self.call_on(self._session, method, params, acts=acts)

    async def call_on(
        self,
        session: str | None,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = COMMAND_TIMEOUT_S,  # noqa: ASYNC109 - the transport's own deadline
        acts: bool = False,
    ) -> dict[str, Any]:
        """`acts`: this call IS the verb's action (a press, a key, the text, a value, a wheel);
        once it has answered, the verb is done even if its tab closes right after (`handle`). A
        verb in parts acts only with its last one - a `fill`'s last field, the Enter of a submit -
        so a tab that closes after its first part never reports a half-filled form as done."""
        result = await self.cdp.call(method, params, session_id=session, timeout=timeout)
        if acts and session == self._session:
            self._acted = True
        return result

    async def handle(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """One verb. A refused call (`CdpError`) and a stale reference are results. Anything else
        propagates: a dead connection (`CdpClosed`, `ConnectionError`) for the owner to reconnect,
        and any other exception (a `TimeoutError`, a `KeyError` from a reply missing a field) for
        the OWNER to turn into a result, as `Daemon.dispatch` does."""
        verb = cmd.get("verb", "")
        if verb not in VERBS:
            return {"ok": False, "error": f"unknown verb {verb!r}"}
        self._acted = self.unsettled = False
        before = await watch.probe(self.call, self._main_frame_id) if verb in ACTING_VERBS else None
        if verb in ACTING_VERBS:
            self._arm()  # every act settles on what IT started: an earlier page's load is not its own
        try:
            result: dict[str, Any] = await getattr(self, f"_v_{verb}")(cmd)
            if verb in ACTING_VERBS and result.get("ok"):
                try:
                    result["_record"] = await self._record_frame(cmd)
                except (CdpError, CdpClosed, ConnectionError, TimeoutError):
                    # Evidence is never authority (a frame a click's navigation detached): the act
                    # stands, and a placeholder row (`capture: failed`) keeps the recording's action
                    # count equal to the journal's, which counted this call ok.
                    result["_record"] = {"capture": "failed"}
        except StaleReference as s:
            return {"ok": False, "error": f"stale reference {s.ref}; take a new snapshot"}
        except CdpError as e:
            closed = verb in ACTING_VERBS and self._target is not None and not self._open(self._target)
            if not closed or self._desired() is None:
                return {"ok": False, "error": f"browser refused: {e.message}"}
            # The page's own tab closed under the verb, and the page goes back to the tab that
            # opened it, below. Done when its action landed first (a sign-in popup's last click,
            # no picture of a closed tab); never when the tab closed before it (a page timer).
            # A navigation never acts: into a tab that closed, it loaded nothing anywhere.
            if self._acted:
                result = {"ok": True, "tab": "closed", "_record": {"capture": "failed"}}
            else:
                result = {
                    "ok": False,
                    "error": "the tab closed before the action; nothing was done",
                    "tab": "closed",
                }
        except (CdpClosed, ConnectionError):
            raise
        if verb in ACTING_VERBS and self.follow_pending:
            await self._follow_after(result)
        if verb in ACTING_VERBS and result.get("ok"):
            after = await watch.probe(self.call, self._main_frame_id) if before is not None else None
            role, tab = self._meta.get(str(cmd.get("ref")), ("", ""))[0], result.get("tab")
            result |= watch.reported(verb, before, after, tab=tab, settled=not self.unsettled, role=role)
            result["moved"] = watch.left(verb, result, before)
        look = cmd.get("snapshot")
        if result.get("ok") and (look is True or (look == "moved" and result.get("moved"))):
            # The page the model's next call acts on (after any tab move), so it need not ask to look:
            # `"moved"` only when the act left it, as a look expires the references a response holds.
            # A tree that cannot be read (a detached frame) never turns the verb into an error.
            with contextlib.suppress(CdpError, CdpClosed, ConnectionError, TimeoutError):
                page = await self._v_snapshot({})
                result.update({k: page[k] for k in ("epoch", "text", "url", "title", "_targets")})
        return result

    async def _follow_after(self, result: dict[str, Any]) -> None:
        """An acting verb that opened a tab, or closed its own, ends on the tab the page moved
        to, and says so: the agent's next step starts where the page is, still under the verb
        lock (`Daemon.dispatch`)."""
        left = self._target
        with contextlib.suppress(CdpError, TimeoutError):
            if await self.follow():
                result.setdefault("tab", "opened" if left is not None and self._open(left) else "closed")
                result.update(await self._where())

    async def _where(self) -> dict[str, str]:
        hist = await self.call("Page.getNavigationHistory")
        cur = hist["entries"][hist["currentIndex"]]
        return {"url": cur["url"], "title": cur.get("title", "")}

    def _arm(self) -> None:  # before an act that may navigate: what `_settle` hears is the act's
        self._loaded.clear()
        self._navigating = False
        self._nav_started.clear()

    async def _settle(self) -> None:
        """Until the page is done answering the act, and no longer (`watch`): a navigation that
        STARTED (main-frame `Page.frameStartedLoading`) or a loading document is waited out to its
        load (`LOAD_S`), one that never starts (a `preventDefault()`'d submit) is not; then the quiet
        (no mutation for 80 ms, no young request) up to `SETTLE_S`, all under one cap (`unsettled`)."""
        loop = asyncio.get_running_loop()
        cap, deadline = loop.time() + watch.LOAD_S + watch.SETTLE_S, loop.time() + watch.SETTLE_S
        while True:
            if self._nav_started.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._loaded.wait(), timeout=max(cap - loop.time(), 0.0))
                self._nav_started.clear()
                deadline = min(loop.time() + watch.SETTLE_S, cap)
            if (left := deadline - loop.time()) <= 0:
                self.unsettled = deadline >= cap and (self._navigating or not self._loaded.is_set())
                return
            page = await watch.probe(self.call, self._main_frame_id, wait_s=left)
            if page and page.get("loading") and not self._loaded.is_set():
                self._nav_started.set()  # still parsing: waited out to its load, as a navigation is
            elif not self._nav_started.is_set() and not self.requests.fresh(loop.time()):
                return
            await self.requests.wait(deadline - loop.time(), or_=self._nav_started)

    async def _v_open(self, cmd: dict[str, Any]) -> dict[str, Any]:
        url = cmd.get("url")
        if not url:
            return {"ok": False, "error": "missing url"}
        if url != "about:blank" and urlsplit(url).scheme not in OPEN_SCHEMES:
            return {"ok": False, "error": "refused scheme"}
        self._arm()
        error_text = str((await self.call("Page.navigate", {"url": url})).get("errorText") or "")
        await self._settle()
        where = await self._where()
        if error_text or where["url"].startswith("chrome-error://"):
            return watch.open_failed(error_text)
        return {"ok": True, **where}

    async def go_back(self) -> None:
        """A person's way back from a page (the screen's Back): the page's previous
        history entry, or - when it has none and other tabs are open - the tab closes, and the
        page returns to its opener (`follow`). Never recorded: a screen is not a verb."""
        hist = await self.call("Page.getNavigationHistory")
        index = hist["currentIndex"]
        if index > 0:
            await self.call("Page.navigateToHistoryEntry", {"entryId": hist["entries"][index - 1]["id"]})
        elif self._target is not None and len(self._tabs) > 1:
            await self.cdp.call("Target.closeTarget", {"targetId": self._target})

    async def _v_back(self, cmd: dict[str, Any]) -> dict[str, Any]:
        hist = await self.call("Page.getNavigationHistory")
        if hist["currentIndex"] == 0:
            return {"ok": True, **await self._where(), "note": "already at the first page"}
        self._arm()
        await self.call(
            "Page.navigateToHistoryEntry", {"entryId": hist["entries"][hist["currentIndex"] - 1]["id"]}
        )
        await self._settle()
        return {"ok": True, **await self._where()}

    async def _v_wait(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """Until `for`, a text, shows (up to `WAIT_S`): read as the page changes and after a load, never a sleep."""
        text = cmd.get("for")
        if not isinstance(text, str) or not text:
            return {"ok": False, "error": "wait needs a text that should appear on the page"}
        loop = asyncio.get_running_loop()
        deadline, seen = loop.time() + WAIT_S, None
        while seen is None and (left := deadline - loop.time()) > 0:
            self._loaded.clear()
            if (seen := await watch.appears(self.call, self._main_frame_id, text, left)) is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._loaded.wait(), timeout=left)
        if seen:
            return {"ok": True}
        return {"ok": False, "error": f"waited {WAIT_S:g} s and {text!r} did not appear"}

    async def _v_snapshot(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """The model's look (lean, `tree.py`); with `cap`, the proof check's read - the tree as
        written, never from the model, whose tool builds its own command; with `whole`, the page
        for a file, with no references, the model's own left live."""
        tree = (await self.call("Accessibility.getFullAXTree"))["nodes"]
        passwords = await self._password_backend_ids(tree)
        find = cmd.get("find") if isinstance(cmd.get("find"), str) else None
        if cmd.get("whole"):
            snap = render(tree, epoch=None, cap=SNAPSHOT_CHARS_MAX, password_backend_ids=passwords, lean=True)
            return {"ok": True, "text": snap.text, **await self._where()}
        self._epoch += 1
        cap = cmd.get("cap")  # the proof check's read: the tree as written, as long as it asks
        system = cap if type(cap) is int and 0 < cap <= SNAPSHOT_CHARS_MAX else None
        look: dict[str, Any] = {}
        if not system:
            look = {"lean": True, "screen": await self._screen(), "previous": self._seen}
            self._seen = interactive_ids(tree)
        size = system or self.snapshot_chars
        snap = render(tree, epoch=self._epoch, cap=size, password_backend_ids=passwords, find=find, **look)
        self._refs, self._meta = snap.refs, snap.meta
        answer = {"epoch": self._epoch, "text": snap.text, "_targets": snap.meta}
        proof = proof_read(snap, system) if system else {}
        return {"ok": True, **answer, **proof, **await self._where()}

    async def _screen(self) -> Screen | None:
        """Where the page's nodes sit against the viewport; None when the browser cannot say."""
        with contextlib.suppress(CdpError):
            dom = await self.call("DOMSnapshot.captureSnapshot", {"computedStyles": []})
            return screen_of(dom, await self.call("Page.getLayoutMetrics"))
        return None

    async def _password_backend_ids(self, tree: list[dict[str, Any]]) -> set[int]:
        out: set[int] = set()
        for n in tree:
            if n.get("role", {}).get("value") != "textbox" or not n.get("backendDOMNodeId"):
                continue
            desc = await self.call("DOM.describeNode", {"backendNodeId": n["backendDOMNodeId"]})
            attrs = desc["node"].get("attributes", [])
            pairs = dict(zip(attrs[::2], attrs[1::2], strict=False))
            if pairs.get("type", "").lower() == "password":
                out.add(int(n["backendDOMNodeId"]))
        return out

    def _backend(self, ref: str) -> int:
        if ref not in self._refs:
            raise StaleReference(ref)
        return self._refs[ref]

    async def _quad(self, backend_id: int) -> list[float]:
        """The element's content quad, scrolled into view first (the click's own read)."""
        await self.call("DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend_id})
        return (await self.call("DOM.getBoxModel", {"backendNodeId": backend_id}))["model"]["content"]

    async def _center(self, backend_id: int) -> tuple[float, float]:
        return _center_of(await self._quad(backend_id))

    async def _box(self, backend_id: int) -> dict[str, float] | None:
        """The element's box for `screen.action`, read WITHOUT scrolling - the verb already put it
        where it is, and typing never scrolled before. Evidence, never authority: a node that has
        no box (detached, hidden) answers None, never an error the model would retry. A dead
        browser still raises (`CdpClosed`): that is a crash, not a missing box."""
        try:
            quad = (await self.call("DOM.getBoxModel", {"backendNodeId": backend_id}))["model"]["content"]
        except (CdpError, KeyError, TypeError):
            return None
        return _box_of(quad)

    async def _mouse(self, x: float, y: float) -> None:
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            await self.call(
                "Input.dispatchMouseEvent",
                {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1},
                acts=kind != "mouseMoved",  # a pointer that only moved clicked nothing
            )

    async def _focused(self) -> dict[str, Any]:
        """`document.activeElement`, resolved once: its CDP `objectId` (for `_v_inspect`'s
        JavaScript read) and its live role/name (for `_check_expect`'s no-ref case - `press`,
        and `scroll` with no `ref`, act on whatever has focus, not on any snapshot reference)."""
        focused = await self.call(
            "Runtime.evaluate", {"expression": "document.activeElement", "objectGroup": "inspect"}
        )
        object_id = focused["result"].get("objectId")
        role, name = "", ""
        if object_id:
            desc = await self.call(
                "Accessibility.getPartialAXTree", {"objectId": object_id, "fetchRelatives": False}
            )
            nodes: list[dict[str, Any]] = desc.get("nodes") or []
            if nodes:
                role = str(cast("dict[str, Any]", nodes[0].get("role") or {}).get("value") or "")
                name = str(cast("dict[str, Any]", nodes[0].get("name") or {}).get("value") or "")
        return {"object_id": object_id, "role": role, "name": name}

    async def _live_role_name(self, ref: str) -> tuple[str, str]:
        """The live role/name of `ref`'s element right now - never the snapshot-time cache in
        `_meta` (spec: the approved call must match the LIVE target). `_meta` is only the
        fallback when the accessibility tree has nothing to say about the node (e.g. detached).
        An empty `ref` means "no snapshot reference" - `press` and a ref-less `scroll` act on
        whatever currently has focus, so that is what gets fingerprinted. A NON-empty `ref` that
        is not live is a different case entirely - a stale reference, which must fail as stale
        (`self._backend` raises `StaleReference`), never be silently read as "the focused
        element" and reported as an "intervened" mismatch instead."""
        if not ref:
            focused = await self._focused()
            return focused["role"], focused["name"]
        backend_id = self._backend(ref)
        desc = await self.call(
            "Accessibility.getPartialAXTree", {"backendNodeId": backend_id, "fetchRelatives": False}
        )
        nodes: list[dict[str, Any]] = desc.get("nodes") or []
        if nodes:
            role = str(cast("dict[str, Any]", nodes[0].get("role") or {}).get("value") or "")
            name = str(cast("dict[str, Any]", nodes[0].get("name") or {}).get("value") or "")
            return role, name
        return self._meta.get(ref, ("", ""))

    async def _check_expect(self, cmd: dict[str, Any]) -> dict[str, Any] | None:
        """The approved call must land where it was approved."""
        expect = cmd.get("expect")
        if not expect:
            return None
        ref = str(cmd.get("ref") or "")
        role, name = await self._live_role_name(ref)
        where = await self._where()
        parts = urlsplit(where["url"])
        actual = {"role": role, "name": name, "host": parts.hostname or "", "path": parts.path or ""}
        for key in ("role", "name", "host", "path"):
            if key in expect and str(expect[key]) != actual[key]:
                return {"ok": False, "error": "intervened", "expected": expect, "actual": actual}
        return None

    async def _v_click(self, cmd: dict[str, Any]) -> dict[str, Any]:
        ref = cmd.get("ref")
        if not ref:
            return {"ok": False, "error": "missing ref"}
        backend_id = self._backend(ref)  # a stale ref must fail as stale, not as "intervened"
        if bad := await self._check_expect(cmd):
            return bad
        quad = await self._quad(backend_id)
        x, y = _center_of(quad)
        self._arm()
        await self._mouse(x, y)
        await self._settle()
        return {"ok": True, **await self._where(), "box": _box_of(quad)}

    async def _focus(self, backend_id: int) -> None:
        await self.call("DOM.focus", {"backendNodeId": backend_id})

    async def _set_value(self, backend_id: int, value: str, *, acts: bool = True) -> None:
        obj = await self.call("DOM.resolveNode", {"backendNodeId": backend_id})
        await self.call(
            "Runtime.callFunctionOn",
            {
                "objectId": obj["object"]["objectId"],
                "functionDeclaration": "function(v){this.value=v;this.dispatchEvent(new Event('input',{bubbles:true}));this.dispatchEvent(new Event('change',{bubbles:true}));}",
                "arguments": [{"value": value}],
            },
            acts=acts,
        )

    async def _type_text(self, backend_id: int, text: str, *, acts: bool = True) -> str | None:
        """Real keyboard input, not `_set_value`: clear via the DOM (simplest reliable clear),
        then `Input.insertText` so frameworks with a native value tracker (React et al.) see the
        change as genuine input rather than a programmatic value assignment they'd ignore.
        `acts=False` when more of the verb follows (another field, the Enter of a submit).
        A WHOLE-value input (`WHOLE_VALUE_FORMATS`) takes its value set instead; the error when it
        refused `text`, else None."""
        await self._focus(backend_id)
        obj = await self.call("DOM.resolveNode", {"backendNodeId": backend_id})
        out = await self.call(
            "Runtime.callFunctionOn",
            {
                "objectId": obj["object"]["objectId"],
                "functionDeclaration": WHOLE_VALUE_JS,
                "arguments": [{"value": text}],
                "returnByValue": True,
            },
        )
        whole: dict[str, Any] = cast("dict[str, Any]", out.get("result") or {}).get("value") or {}
        if whole.get("whole"):
            if acts and self._session is not None:
                self._acted = True  # setting it was the verb's action, as the typing would be
            kind = str(whole.get("type", ""))
            if text and not whole.get("value"):
                return f"the {kind} field takes {WHOLE_VALUE_FORMATS.get(kind, 'its own format')}; {text!r} did not set it"
            return None
        await self._set_value(backend_id, "", acts=False)  # the clear is not the typing
        await self.call("Input.insertText", {"text": text}, acts=acts)
        return None

    async def _key(self, key: str) -> None:
        down: dict[str, Any] = {"type": "keyDown", "key": key, "code": key}
        if key == "Enter":
            # Blink submits a form on the keypress event, which needs charCode 13 - key/code alone
            # dispatch a keydown/keyup pair with no keypress, so nothing fires.
            down |= {"text": "\r", "unmodifiedText": "\r", "windowsVirtualKeyCode": 13}
        await self.call("Input.dispatchKeyEvent", down, acts=True)
        await self.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": key}, acts=True)

    async def _enter(self) -> None:
        await self._key("Enter")

    async def _v_type(self, cmd: dict[str, Any]) -> dict[str, Any]:
        ref = cmd.get("ref")
        if not ref:
            return {"ok": False, "error": "missing ref"}
        bid = self._backend(ref)  # a stale ref must fail as stale, not as "intervened"
        if bad := await self._check_expect(cmd):
            return bad
        if refused := await self._type_text(bid, str(cmd.get("text", "")), acts=not cmd.get("submit")):
            return {"ok": False, "error": refused}
        box = await self._box(bid)  # before a submit: a navigation takes the field away
        if cmd.get("submit"):
            self._arm()
            await self._enter()
        await self._settle()  # a submit's page, or the suggestions a field offers as it is typed in
        return {"ok": True, **await self._where(), **({"box": box} if box else {})}

    async def _v_fill(self, cmd: dict[str, Any]) -> dict[str, Any]:
        fields: list[dict[str, Any]] = cmd.get("fields") or []
        if not fields:
            return {"ok": False, "error": "missing fields"}
        refs = [str(field.get("ref") or "") for field in fields]
        if not all(refs):
            return {"ok": False, "error": "missing ref"}
        # Every field resolves (a stale ref must fail as stale, not as "intervened") and is
        # checked against its OWN fingerprint - `expect` is keyed by ref - before the first
        # keystroke: Enter lands in the last field's form, so the binding covers every field.
        backend_ids = [self._backend(ref) for ref in refs]
        expect: dict[str, Any] = cmd.get("expect") or {}
        for ref in refs:
            if bad := await self._check_expect({"expect": expect.get(ref), "ref": ref}):
                return bad
        last = len(fields) - 1
        for i, (backend_id, field) in enumerate(zip(backend_ids, fields, strict=True)):
            if refused := await self._type_text(
                backend_id, str(field.get("value", "")), acts=i == last and not cmd.get("submit")
            ):
                # The fields before it are filled; nothing is submitted with a field left wrong.
                return {"ok": False, "error": f"field {refs[i]}: {refused}"}
        box = await self._box(backend_ids[-1])  # the last field typed into, before a submit moves on
        if cmd.get("submit"):
            await self._focus(backend_ids[-1])
            self._arm()
            await self._enter()
        await self._settle()
        return {"ok": True, **await self._where(), **({"box": box} if box else {})}

    async def _v_select(self, cmd: dict[str, Any]) -> dict[str, Any]:
        ref = cmd.get("ref")
        if not ref:
            return {"ok": False, "error": "missing ref"}
        bid = self._backend(ref)  # a stale ref must fail as stale, not as "intervened"
        if bad := await self._check_expect(cmd):
            return bad
        await self._set_value(bid, str(cmd.get("value", "")))
        await self._settle()  # a sort or a filter reloads the list it sits over
        box = await self._box(bid)
        return {"ok": True, **await self._where(), **({"box": box} if box else {})}

    async def _v_press(self, cmd: dict[str, Any]) -> dict[str, Any]:
        if bad := await self._check_expect(cmd):
            return bad
        key = str(cmd.get("key", "Enter"))
        self._arm()
        await self._key(key)
        await self._settle()
        return {"ok": True, **await self._where()}

    async def _v_scroll(self, cmd: dict[str, Any]) -> dict[str, Any]:
        ref = cmd.get("ref")
        # A stale ref must fail as stale, not as "intervened" - resolve (and so stale-check) it
        # before `_check_expect` runs, exactly like click/type/select/fill.
        backend_id = self._backend(ref) if ref else None
        if bad := await self._check_expect(cmd):
            return bad
        dy = -600 if cmd.get("direction") == "up" else 600
        x, y = (640.0, 450.0)
        if backend_id is not None:
            x, y = await self._center(backend_id)
        await self.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": dy},
            acts=True,
        )
        await self._settle()
        return {"ok": True, **await self._where()}

    async def _v_screenshot(self, cmd: dict[str, Any]) -> dict[str, Any]:
        shot = await self.call("Page.captureScreenshot", {"format": "png"})
        return {"ok": True, "png_b64": shot["data"], **await self._where()}

    async def _v_inspect(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """Read-only: describes a ref (else the focused element), its form, a select value's option text, or
        each of a fill's `refs`; `INSPECT_JS` is a fixed constant, never built from model text."""
        refs = cmd.get("refs")
        if not isinstance(refs, list):
            return await self._inspect_one(str(cmd.get("ref") or ""), cmd.get("value"))
        each = [await self._inspect_one(str(ref), None) for ref in cast("list[object]", refs)]
        return next((info for info in each if not info["ok"]), {"ok": True, "each": each})

    async def _inspect_one(self, ref: str, value: object) -> dict[str, Any]:
        if ref:
            backend_id = self._backend(ref)
            role, name = await self._live_role_name(ref)
            resolved = await self.call("DOM.resolveNode", {"backendNodeId": backend_id})
            object_id = resolved["object"].get("objectId")
        else:
            # `_focused()` takes the `objectId` straight off `getPartialAXTree` (no
            # `getAXNodeAndAncestors` round trip through a DOM node id): all a role/name read needs.
            focused = await self._focused()
            role, name, object_id = focused["role"], focused["name"], focused["object_id"]
        info: dict[str, Any] = {"form_labels": [], "submit_name": "", "option": ""}
        info |= dict.fromkeys(("submits", "navigates", "form_navigates"), False)
        if object_id:
            call: dict[str, Any] = {
                "objectId": object_id,
                "functionDeclaration": INSPECT_JS,
                "returnByValue": True,
            }
            if value is not None:
                call["arguments"] = [{"value": str(value)}]
            out = await self.call("Runtime.callFunctionOn", call)
            if out.get("exceptionDetails"):
                # Fail closed: a form the script could not read is not a form without a submit
                # control, and answering "no form" let the danger check read nothing and act.
                return {"ok": False, "error": "inspect failed: the page script threw"}
            info = out["result"].get("value") or info
        parts = urlsplit((await self._where())["url"])
        at = {"host": parts.hostname or "", "path": parts.path or ""}
        return {"ok": True, "role": role, "name": name, **at, **info}

    # ── recording ────────────────────────────────────────────────────────────────────────

    async def _record_frame(self, cmd: dict[str, Any]) -> dict[str, Any]:
        # No `clip`: clip coordinates are document-relative, so after scrolling they capture
        # whatever used to be at the top of the page instead of what is on screen now.
        shot = await self.call("Page.captureScreenshot", {"format": "jpeg", "quality": 50})
        target = ""
        if cmd.get("ref") and cmd["ref"] in self._refs:
            target = cmd["ref"]  # the daemon swaps this for the role+name it saw in the last snapshot
        return {"target": target, "jpeg": base64.b64decode(shot["data"])}
