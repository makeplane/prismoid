# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Whose each target is: a target Chrome attached held at its start is
given an owner and fenced before it runs. A layer of `Chrome`, whose module docstring says why.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any, Final

from prismoid.device.chrome_pipe import ALL_REQUESTS, HELD, ChromeError, ChromeRefused
from prismoid.device.chrome_requests import NO_ONES_WORKERS, ChromeRequests
from prismoid.device.chrome_types import BASE, BASE_VERDICT, Verdict

# `Network` is on for its announcements only: bodies stay out of the renderer's buffers, and out
# of `requestWillBeSent`. Not 0: to Chrome that is no limit at all.
_NETWORK: Final = {"maxTotalBufferSize": 1024, "maxResourceBufferSize": 1024, "maxPostDataSize": 1024}


class ChromeTargets(ChromeRequests):
    """A target held at its start, until it has an owner and a fence."""

    def _on_attached(self, parent: str | None, params: dict[str, Any]) -> None:
        session, info = params.get("sessionId"), params.get("targetInfo") or {}
        target = info.get("targetId")
        if not isinstance(session, str) or not isinstance(target, str):
            return
        self._info[target] = info
        if parent is None:
            if target in self._fence_of:
                return  # one more session on a page already fenced: a relay's own attach
            if info.get("type") == "page":  # no worker's, from before anything waits: `_no_fence_saw`
                self._fire("Network.enable", _NETWORK, session)
                self._fire("Network.setBypassServiceWorker", {"bypass": True}, session)
                if target not in self._verdicts:
                    self._verdicts[target] = asyncio.get_running_loop().create_future()
            self._attached.add(session)
            self._spawn(self._adopt(session, info))
        elif parent in self._fenced:
            self._attached.add(session)
            if (page := self._page_of.get(parent)) is not None:
                self._page_of[session] = page
                if info.get("type") != "iframe":
                    self._workers.add(session)
            self._spawn(self._fence_child(session, info, self._fenced[parent]))

    def _on_detached(self, parent: str | None, params: dict[str, Any]) -> None:
        session = params.get("sessionId")
        if not isinstance(session, str):
            return
        self._attached.discard(session)
        self._fenced.pop(session, None)
        self._page_of.pop(session, None)
        self._iframes.pop(session, None)
        self._workers.discard(session)
        if parent is None and (relay := self._bound.pop(session, None)) is not None:
            relay.on_event({"method": "Target.detachedFromTarget", "params": params})

    async def _adopt(self, session: str, info: dict[str, Any]) -> None:
        """A target Chrome attached held at its start: fence it as its task's, or let it go.

        A page is the task's to drive when the task made it or its page opened it. It is fenced as
        a task's whenever it is in that task's window, whoever opened it: a Cmd- or middle-click, or
        the person's own Cmd-T there, opens a tab with no opener. A page in no task's window fails
        closed to the base list: a page can turn the agent's plain click into a Shift-click (a
        synthetic `MouseEvent`) that opens a new window naming no opener, and Chrome's
        `Page.windowOpen` does not fire for it (checked on Chrome 156), so nothing attributes it."""
        target, kind = str(info["targetId"]), info.get("type")
        verdict = self._verdicts.get(target)

        def decide(how: Verdict) -> None:
            if verdict is not None and not verdict.done():
                verdict.set_result(how)

        try:
            if kind == "page":
                await self._adopt_page(session, info, decide)
                return
            elif kind in NO_ONES_WORKERS:  # a blob or a data one is as much a web's
                await self._fence_child(session, info, None)
                return
            # Not a page and no web worker: it runs as without Prismoid.
            self._fire("Runtime.runIfWaitingForDebugger", {}, session)
            self._fire("Target.detachFromTarget", {"sessionId": session})
        finally:
            decide(BASE_VERDICT)  # an adoption cut short fails closed

    async def _adopt_page(
        self, session: str, info: dict[str, Any], decide: Callable[[Verdict], None]
    ) -> None:
        """A page: its verdict, and then its fence, by the rules `_adopt` gives."""
        target = str(info["targetId"])
        if self._marked(info):
            self._tainted.add(target)
        owner = self._owner.get(str(info.get("openerId") or ""))
        if owner is None and self._creating.locked():
            async with self._creating:  # a window being made: its call has to answer first
                pass
        owner = owner or self._claims.pop(target, None)
        if owner is not None:
            if self._carries(owner, str(info.get("openerId"))):
                self._handed[owner].add(target)  # a sign-in's popup
            decide((True, owner))
            await self._fence_page(session, target, owner)
            return
        try:
            window = await self._window_of(target)
        except (ChromeError, TimeoutError, KeyError, ValueError):
            window = None  # nowhere to be found: no task's window
        if window is not None and (task := self._task_windows.get(window)) is not None:
            decide((True, task))
            await self._fence_page(session, target, task, driven=False)
            return
        decide(BASE_VERDICT)
        self._fire("Page.enable", {}, session)  # `_no_fence_saw`
        await self._fence_child(session, info, BASE)
        await self._landed_unheard(session, BASE)

    def _still_here(self, session: str, target: str) -> bool:
        """A target can attach and go before its fencing starts, the two read off the pipe together,
        and nothing that comes later would clean up what fencing it wrote down."""
        return session in self._attached and target in self._info

    async def _window_of(self, target: str) -> int:
        return int((await self.call("Browser.getWindowForTarget", {"targetId": target}))["windowId"])

    async def _fence_page(self, session: str, target: str, owner: str, *, driven: bool = True) -> None:
        """A task's page: its requests vetted, its iframes and workers held for the same, then run.
        A page the task drives also marks its window as the task's, before it runs; one fenced only
        for the window it is in (`driven=False`) is never handed to the task's agent."""
        if not self._still_here(session, target):
            return
        if target in self._tainted:
            self.take_back(owner)
        self._fenced[session], self._fence_of[target], self._page_task[target] = owner, session, owner
        self._page_of[session] = target
        if driven:
            self._owner[target] = owner
        try:
            await self.call("Fetch.enable", {"patterns": ALL_REQUESTS}, session=session)
            # Its commits, for `_on_committed`; `Network` is on from its attach, for `_on_announced`.
            # Fired, not awaited: a page held at its start answers this only once it runs, and sent
            # before the run below, so nothing goes unheard.
            self._fire("Page.enable", {}, session)
            await self.call("Target.setAutoAttach", HELD, session=session)
            if driven:  # a window that cannot be read is no task's: its other tabs get the base list
                with contextlib.suppress(ChromeRefused, TimeoutError, KeyError, ValueError):
                    self._task_windows[await self._window_of(target)] = owner
            await self.call("Runtime.runIfWaitingForDebugger", session=session)
        except (ChromeError, TimeoutError):
            return  # it closed while held; its detach and destroy clean up
        if not driven:
            await self._landed_unheard(session, owner)
            return
        if (ready := self._ready.get(target)) is not None and not ready.done():
            ready.set_result(None)
        opening = self._opening.get(owner)
        if (
            opening is not None
            and opening[0] == self._info.get(target, {}).get("openerId")
            and not opening[1].done()
        ):
            opening[1].set_result(target)
        for relay in tuple(self._relays.get(owner, ())):
            relay.on_joined(self._info.get(target, {"targetId": target, "type": "page"}))

    async def _landed_unheard(self, session: str, ref: str) -> None:
        """A tab with no opener can commit before its `Page.enable`: no event will say so."""
        with contextlib.suppress(ChromeError, TimeoutError, KeyError):
            tree = await self.call("Page.getFrameTree", session=session)
            self._no_fence_saw(ref, session, tree["frameTree"]["frame"])

    async def _fence_child(self, session: str, info: dict[str, Any], owner: str | None) -> None:
        """An iframe or a worker: fenced as `owner` before it runs. One that cannot be fenced stays
        held, never run - except a dedicated worker, whose requests its page's fence pauses too."""
        if not self._still_here(session, str(info["targetId"])):
            return
        self._fenced[session] = owner
        iframe, tasks = info.get("type") == "iframe", session in self._page_of
        if iframe or tasks:  # as a page: a task's iframe or its worker, and any iframe bypasses
            self._fire("Network.enable", _NETWORK, session)
        if iframe:
            self._fire("Network.setBypassServiceWorker", {"bypass": True}, session)
        if iframe and tasks:
            self._iframes[session] = str(info["targetId"])
            self._fire("Page.enable", {}, session)
        try:
            await self.call("Fetch.enable", {"patterns": ALL_REQUESTS}, session=session)
        except (ChromeError, TimeoutError):
            if info.get("type") != "worker":
                return
        with contextlib.suppress(ChromeError, TimeoutError):
            await self.call("Target.setAutoAttach", HELD, session=session)
        with contextlib.suppress(ChromeError, TimeoutError):
            await self.call("Runtime.runIfWaitingForDebugger", session=session)
