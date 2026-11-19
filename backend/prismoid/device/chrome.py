# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Prismoid Chrome: one Google Chrome per helper, on its own
profile, a window per browser task, and the inner layer of the browser's egress fence.

* **Its own instance.** `--user-data-dir=<home>/chrome` (which the fence read-denies to agent
  commands) makes a separate Chrome beside the person's everyday one, whatever that has open;
  Chrome 136+ refuses automation on the default profile anyway. DevTools rides
  `--remote-debugging-pipe`: no port exists for anything else on the Mac to attach to, and Chrome
  exits when the helper's end of the pipe closes. It starts on the first browser frame, stops with
  the helper, and comes back on the next frame if it died.
* **The proxy's 407, answered over CDP.** `--proxy-server` takes no credential, so the browser-wide
  session pauses every request with `handleAuthRequests`: our proxy's challenge gets the `chrome`
  principal (its token lives in this process only, one per helper life), any other - a site's own
  sign-in - is Chrome's own prompt to the person (`Default`).
* **A window per task, one shared profile.** Not a browser context per task: it is
  incognito-shaped and loses the logins that are the profile's point ("Logins the user makes
  in it persist across tasks"). So what keeps tasks apart is whose each target is: every new page
  is attached held at its start (`waitForDebuggerOnStart`), given an owner - the task its window
  was made for, or whose page opened it - and fenced (`Fetch.enable` on its own session, each
  request vetted by `fence`) before it runs; its iframes and workers are attached the same way
  under it. A page in a task's window is fenced as that task's whoever opened it (a Cmd-click,
  the person's Cmd-T). A page in no task's window - the person's Cmd-N, or a new window a page
  conjured from the agent's plain click with a synthetic Shift-click - is held to the base list
  only (`CHROME_BASE`, empty): it fails closed, since nothing says whose it is. A service worker,
  which belongs to no one task, is held to the union of the live lists - the proxy's own outer
  rule - and kept off loopback.
* **Loopback goes to the proxy too**, which refuses it: `Fetch` never sees a WebSocket's handshake,
  so without that a page's socket could reach any port on this Mac, Prismoid's own included.
* **Two doors on the proxy** (ADR-078, `egress.py`): `https:` to one, `http:` and every
  WebSocket to the other, so that a WebSocket can never ride a tunnel a page's asset opened. Chrome
  answers the 407 of a WebSocket or a cross-origin image from its cache or fails, and no CDP event
  fires, so the first window seeds that cache with one plain request to each door (`_seed_doors`).
* **A hand-off's window is the person's.** A sign-in leaves for a site no list
  names (Sign in with Amazon, a company's SSO), so while a task's agent is parked on its person, its
  pages may go anywhere public (`hand_over`), and the first thing the agent does ends it, as does
  anything that says nobody is parked any more, or `PASS_S`. A page it left off the list goes back
  to a listed one before the agent reads (`take_back`), and the pass is never given while a page a
  fenced command could have written, or one it opened, is open (`_written`).
* **Which page asked.** An asset is excused only on a page already on its task's list, so each
page's committed top-level URL is kept from `Page.frameNavigated`, and only
  from that: `Target` info can lag a commit. A main-frame Document request forgets it until that
  navigation commits, so a page mid-swap, and a page never seen to commit, have no URL to be excused
  by. A commit the newest navigation does not explain is not believed. Under load a new page's first
  assets can beat word of its commit, so such a request waits for the commit (`_pages_of`), and is
  excused only if its `Referer` shows it came from the page that landed: the page being left can
  still be running, and it must not borrow its successor's URL. A navigation Chrome cancels (a 204,
  a download, a `window.stop()`) never commits and leaves the page where it was, so the URL it last
  committed comes back (`_on_abandoned`), unless some other commit landed meanwhile.
* **Which document asked.** A dedicated worker's requests are paused on its page's session, under
  its page's frame, so `Fetch` cannot tell them from the page's, and a worker outlives the document
  that made it. `Network` can: each session announces its own requests, a document's with its frame
  and `loaderId`, and `Fetch` names the announcement in `networkId`. So an excuse needs a request
  announced by a document that is in the page now (`_document_of`, `_documents`). A worker's session
  has no document to name, so nothing it asks for is excused.
* **A message too large to read.** Chrome writes a request's whole body into `Fetch.requestPaused`,
  twice over, and `Fetch` offers no cap (`Network` does: `_NETWORK`), so a page's upload can be any
  size on the one pipe every task shares. Over `MAX_MESSAGE` a message is not read: its middle is
  dropped as it passes (`_Frames`), and what its two ends name is answered (`_too_large`). A request
  is refused, its pause never left open; a call fails; an event goes unheard, which leaves whatever
  it would have told unknown, and an unknown refuses.
* **Chrome's own background traffic** is turned off by flags rather than allowlisted.

`cdp_relay.TaskRelay` is one task's agent's view of all this: it reaches its own targets only.

`Chrome` is one object told in layers, each reading only the one below, all sharing the one state
table of `ChromePipe._reset`: the process and the pipe (`chrome_pipe.py`), what each page holds and
the hand-off (`chrome_pages.py`), the verdict on each request (`chrome_requests.py`), whose each
target is (`chrome_targets.py`), and here what each message means and a window per task. Import
from this module; the rest are its parts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, Final

from prismoid.device.chrome_pipe import (
    GONE,
    LAUNCH_FLAGS,
    LOG,
    TOO_LARGE,
    ChromeError,
    ChromeMissing,
    ChromeRefused,
    chrome_env,
    missing,
)
from prismoid.device.chrome_targets import ChromeTargets
from prismoid.device.chrome_types import BASE, BASE_VERDICT, Document, Fence, HandedOf, Relay, TopOf
from prismoid.device.egress import SEED_URLS

__all__ = [
    "BASE",
    "GONE",
    "LAUNCH_FLAGS",
    "TOO_LARGE",
    "Chrome",
    "ChromeError",
    "ChromeMissing",
    "ChromeRefused",
    "Fence",
    "HandedOf",
    "Relay",
    "TopOf",
    "chrome_env",
    "missing",
]

_OPEN_S: Final = 10.0  # a window, or a tab, to be made and fenced


class Chrome(ChromeTargets):
    """The Prismoid Chrome, one per helper."""

    # ── what Chrome says ─────────────────────────────────────────────────────────────────────

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if "id" in msg:
            if (reply := self._pending.pop(msg["id"], None)) is not None:
                reply(msg)
            return
        method, session = msg.get("method"), msg.get("sessionId")
        params: dict[str, Any] = msg.get("params") or {}
        match method:
            case "Fetch.requestPaused":
                self._on_paused(session, params)
                return
            case "Fetch.authRequired":
                if session is None:
                    self._on_auth(params)
                return
            case "Page.frameNavigated":
                self._on_committed(session, params.get("frame") or {})
            case "Page.frameAttached" | "Page.frameDetached":
                self._on_frame_life(session, params)
            case "Network.requestWillBeSent":
                self._on_announced(session, params)
            case "Network.loadingFailed":
                self._on_abandoned(session, params)
            case "Target.attachedToTarget":
                self._on_attached(session, params)
            case "Target.detachedFromTarget":
                self._on_detached(session, params)
            case "Target.targetCreated" | "Target.targetInfoChanged":
                self._on_target_info(msg, params)
            case "Target.targetDestroyed" | "Target.targetCrashed":
                self._on_target_ended(msg, params)
            case _:
                pass
        if session is not None and (relay := self._bound.get(session)) is not None:
            relay.on_event(msg)

    def _on_target_info(self, msg: dict[str, Any], params: dict[str, Any]) -> None:
        info = params.get("targetInfo") or {}
        if isinstance(target := info.get("targetId"), str):
            self._info[target] = info
            if msg.get("method") == "Target.targetInfoChanged":
                self._tell_task(target, msg)

    def _on_target_ended(self, msg: dict[str, Any], params: dict[str, Any]) -> None:
        if not isinstance(target := params.get("targetId"), str):
            return
        self._tell_task(target, msg)
        if msg.get("method") == "Target.targetDestroyed":
            self._forget(target)

    def _forget(self, target: str) -> None:
        """A target that is gone: nothing of it is kept, and no hand-off holds it."""
        self._info.pop(target, None)
        self._owner.pop(target, None)
        self._fence_of.pop(target, None)
        self._page_task.pop(target, None)
        # A request waiting on this page's verdict has no page left to wait for.
        verdict = self._verdicts.pop(target, None)
        if verdict is not None and not verdict.done():
            verdict.set_result(BASE_VERDICT)
        self._tops.pop(target, None)
        self._last_listed.pop(target, None)
        self._fell_from.pop(target, None)
        self._navigating.pop(target, None)
        self._landed.pop(target, None)
        self._documents.pop(target, None)
        self._parents.pop(target, None)
        self._wrote.discard(target)
        self._tainted.discard(target)
        for pages in self._handed.values():
            pages.discard(target)

    def _tell_task(self, target: str, msg: dict[str, Any]) -> None:
        if (owner := self._owner.get(target)) is not None:
            for relay in tuple(self._relays.get(owner, ())):
                relay.on_event(msg)

    # ── a window per task ────────────────────────────────────────────────────────────────────

    def _task_lock(self, ref: str) -> asyncio.Lock:
        return self._task_locks.setdefault(ref, asyncio.Lock())

    def _live_page(self, ref: str) -> str | None:
        first = self._windows.get(ref)
        if first is not None and self._owner.get(first) == ref:
            return first
        return next((t for t, owner in self._owner.items() if owner == ref), None)

    async def window_for(self, ref: str) -> str:
        """The task's window, made (and Chrome launched) on first use: its first page's target id.
        A task whose every tab was closed gets a new window."""
        await self.start()
        async with self._task_lock(ref):
            if ref in self._closed:
                raise ChromeError("the task's window closed")
            if (live := self._live_page(ref)) is not None:
                self._windows[ref] = live
                return live
            async with self._creating:
                try:
                    made = await self.call("Target.createTarget", {"url": "about:blank", "newWindow": True})
                except TimeoutError:
                    raise ChromeError("the task's window did not open") from None
                target = str(made["targetId"])
                self._claims[target] = ref
                ready = self._ready[target] = asyncio.get_running_loop().create_future()
            try:
                await asyncio.wait_for(ready, _OPEN_S)
            except TimeoutError:
                raise ChromeError("the task's window did not open") from None
            finally:
                self._ready.pop(target, None)
                self._claims.pop(target, None)
            self._windows[ref] = target
            await self._seed_doors(target)
            return target

    async def _seed_doors(self, page: str) -> None:
        """One plain request to each proxy door, once per Chrome, from the first window while it is
        still blank: each 407 is answered over CDP, which puts the credential in Chrome's cache. The
        407 of a WebSocket or a cross-origin image reaches no event and fails, so a page needs it
        there first. If this cannot run, those requests fail: the safe way round, and the log says so."""
        session = self._fence_of.get(page)
        if self._socket_proxy is None or self._seeded or session is None:
            return
        urls = json.dumps(SEED_URLS)
        expression = f"Promise.allSettled({urls}.map(u => fetch(u, {{mode: 'no-cors'}}))).then(() => 0)"
        try:
            world = await self.call(
                "Page.createIsolatedWorld", {"frameId": page, "worldName": "prismoid"}, session=session
            )
            await self.call(
                "Runtime.evaluate",
                {"contextId": world["executionContextId"], "awaitPromise": True, "expression": expression},
                session=session,
                timeout=_OPEN_S,
            )
            self._seeded = True
        except (ChromeError, TimeoutError, KeyError):
            LOG.warning("the proxy's credential was not seeded: this Chrome's WebSockets and images may fail")

    async def open_tab(self, ref: str, url: str) -> str:
        """A new tab in the task's own window. CDP's `Target.createTarget` names no window - Chrome
        puts a tab in the window made last - so the tab is opened from the task's page instead, in
        an isolated world (the page's own scripts cannot intercept it): it lands beside its opener,
        and is the task's by its opener. `url` is the caller's to vet."""
        source = await self.window_for(ref)
        async with self._task_lock(ref):
            session = self._fence_of.get(source)
            if session is None:
                raise ChromeError("the task's window closed")
            made: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            self._opening[ref] = (source, made)
            try:
                world = await self.call(
                    "Page.createIsolatedWorld", {"frameId": source, "worldName": "prismoid"}, session=session
                )
                await self.call(
                    "Runtime.evaluate",
                    {
                        "contextId": world["executionContextId"],
                        "userGesture": True,
                        "expression": f"window.open({json.dumps(url)}, '_blank', 'noopener'); 0",
                    },
                    session=session,
                )
                return await asyncio.wait_for(made, _OPEN_S)
            except TimeoutError:
                raise ChromeError("the new tab did not open") from None
            finally:
                self._opening.pop(ref, None)

    async def close_window(self, ref: str) -> None:
        """The task ended: its relays end and its tabs close (the window with its last). Under the
        task's lock, so no window is being made for it meanwhile, and none is after it: a dial that
        passed its frame check before this gets none, until a frame is made for it again (`reopen`).
        The lock itself stays (another caller may be waiting on it). What a closing tab still asks
        for is refused."""
        async with self._task_lock(ref):
            self._closed.add(ref)
            self._end_pass(ref)
            for relay in tuple(self._relays.pop(ref, ())):
                relay.on_lost("the task's window closed")
            tabs = [t for t, task in self._page_task.items() if task == ref]
            for target in tabs:
                await self._close_tab(target)
            for target in tabs:
                self._owner.pop(target, None)
                self._page_task.pop(target, None)
                self._fence_of.pop(target, None)
            for session in [s for s, task in self._fenced.items() if task == ref]:
                del self._fenced[session]
                self._page_of.pop(session, None)
            for window in [w for w, task in self._task_windows.items() if task == ref]:
                del self._task_windows[window]
            self._windows.pop(ref, None)

    async def _close_tab(self, target: str) -> None:
        if self.running:
            with contextlib.suppress(ChromeError, TimeoutError):
                await self.call("Target.closeTarget", {"targetId": target})

    def reopen(self, ref: str) -> None:
        """A frame was made for this task: it may have a window again."""
        self._closed.discard(ref)

    # ── for the relays ───────────────────────────────────────────────────────────────────────

    def join(self, relay: Relay) -> None:
        self._relays.setdefault(relay.ref, set()).add(relay)

    def leave(self, relay: Relay) -> None:
        self._relays.get(relay.ref, set()).discard(relay)
        for session in [s for s, r in self._bound.items() if r is relay]:
            del self._bound[session]

    def bind(self, session: str, relay: Relay) -> None:
        """A session a relay opened: its events are that relay's."""
        self._bound[session] = relay

    def owner_of(self, target: str) -> str | None:
        return self._owner.get(target)

    def info(self, target: str) -> dict[str, Any]:
        return dict(self._info.get(target, {"targetId": target}))

    def targets_of(self, ref: str) -> list[dict[str, Any]]:
        """The task's pages, its window's first one first."""
        pages = [t for t, owner in self._owner.items() if owner == ref]
        first = self._windows.get(ref)
        pages.sort(key=lambda t: t != first)
        return [self.info(t) for t in pages]

    # ── what Chrome knows of its pages, read-only ────────────────────────────────────────────
    # Copies and answers, never the maps: whoever watches (the tests, a probe) cannot move a page.

    def top_of(self, page: str) -> str | None:
        """The page's committed top-level URL; None: unknown (mid-navigation, or not a page)."""
        return self._tops.get(page)

    def tops(self) -> dict[str, str]:
        """Every page's committed top-level URL."""
        return dict(self._tops)

    def tops_of(self, ref: str) -> dict[str, str]:
        """The committed URLs of the pages held to this task's list."""
        return {page: url for page, url in self._tops.items() if self._page_task.get(page) == ref}

    def pages_of_task(self, ref: str) -> list[str]:
        """The pages held to this task's list."""
        return [page for page, task in self._page_task.items() if task == ref]

    def fence_session(self, page: str) -> str:
        """A fenced page's fence session. KeyError: not a fenced page."""
        return self._fence_of[page]

    def fenced_pages(self) -> frozenset[str]:
        return frozenset(self._fence_of)

    def fenced_sessions(self) -> frozenset[str]:
        """The sessions of the targets being fenced, a page's and a child's: those not yet detached."""
        return frozenset(self._fenced)

    def handed(self, ref: str) -> frozenset[str]:
        """The pages this task's person drives, while it is in a hand-off."""
        return frozenset(self._handed.get(ref, ()))

    def navigating(self) -> frozenset[str]:
        """The pages with a navigation begun and not yet committed or given up."""
        return frozenset(self._navigating)

    def landed(self, page: str) -> bool:
        """Whether the page's newest navigation has committed, or been given up."""
        landed = self._landed.get(page)
        return landed is not None and landed.is_set()

    def documents_of(self, page: str) -> dict[str, Document]:
        """A page's documents now, by frame."""
        return dict(self._documents.get(page, {}))

    def pages_listed(self) -> frozenset[str]:
        """Every page Chrome has told of."""
        return frozenset(t for t, info in self._info.items() if info.get("type") == "page")
