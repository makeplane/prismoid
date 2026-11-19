# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The rig for the Prismoid Chrome fence's unit tests, shared by the
`test_cdp_relay*` files: a scripted Chrome on the DevTools pipe (`tests/support/fakes/cdp_peer.PipePeer`), the Chrome
and its registry built on it, and an agent on a task relay. A plain module, not a test file."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from prismoid.device.cdp_relay import (
    RequestFence,
    TaskRelay,
)
from prismoid.device.chrome import Chrome
from prismoid.device.frames import FrameRegistry
from prismoid.schemas.device import FrameSpecIn
from prismoid.schemas.machines import FrameKind
from tests.support.fakes.cdp_peer import PipePeer

A, B = "device-a", "device-b"
STAYS = "stays"


PROXY_PORT = 47001


TOKEN = "chrome-token-for-this-life"


class FakeBrowser:
    """A scripted Chrome. It makes windows, tabs and sessions, and announces each new target as
    Chrome does: created, then attached and held at its start - before the call that made it has
    answered. A tab opened from a page lands in that page's window."""

    def __init__(self) -> None:
        self.peer = PipePeer(self.answer)
        self._n = 0
        self.target_of: dict[str, str] = {}  # session -> target
        self.guard_of: dict[str, str] = {}  # target -> the session Chrome auto-attached
        self.window_of: dict[str, int] = {}
        # What a `Page.navigate` on a fence's session does, by URL: an `errorText` for one that
        # commits nothing, `STAYS` for one that says it went and did not. Any other commits.
        self.navigation: dict[str, str] = {}

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    async def answer(self, method: str, params: dict[str, Any], session: str | None) -> dict[str, Any]:
        match method:
            case "Target.createTarget":
                target = self._next("T")
                self.window_of[target] = self._n if params.get("newWindow") else max(self.window_of.values())
                await self.announce(target)
                return {"targetId": target}
            case "Target.attachToTarget":
                new = self._next("S")
                self.target_of[new] = params["targetId"]
                info = {
                    "targetId": params["targetId"],
                    "type": "page",
                    "url": "about:blank",
                    "attached": True,
                }
                await self.peer.emit(
                    "Target.attachedToTarget",
                    {"sessionId": new, "targetInfo": info, "waitingForDebugger": False},
                )
                return {"sessionId": new}
            case "Page.createIsolatedWorld":
                return {"executionContextId": 7}
            case "Runtime.evaluate" if "window.open" in str(params.get("expression")):
                opener = self.target_of[str(session)]
                popup = self._next("T")
                self.window_of[popup] = self.window_of[opener]
                await self.announce(popup, opener=opener)
                return {"result": {"type": "number", "value": 0}}
            case "Browser.getWindowForTarget":
                return {"windowId": self.window_of[params["targetId"]]}
            case "Page.navigate" if session in self.guard_of.values():
                return await self._navigate(params["url"], session)
            case _:
                return {}

    async def _navigate(self, url: str, session: str | None) -> dict[str, Any]:
        target, loader = self.target_of[str(session)], self._next("L")
        said: dict[str, Any] = {"frameId": target, "loaderId": loader}
        match self.navigation.get(url):
            case None:
                frame = {"id": target, "url": url, "loaderId": loader}
                await self.peer.emit("Page.frameNavigated", {"frame": frame}, session=session)
            case error if error != STAYS:
                said["errorText"] = error
            case _:
                pass
        return said

    async def announce(
        self,
        target: str,
        *,
        opener: str | None = None,
        kind: str = "page",
        url: str = "about:blank",
        window: int | None = None,
        reference: bool = True,
    ) -> str:
        """A target made, then attached held at its start. `window`: the window it lands in (a
        tab opened from a page lands in the page's; one in no window has none). `reference`: false
        for one opened `noopener`, which Chrome still names an opener for."""
        if window is not None:
            self.window_of[target] = window
        info: dict[str, Any] = {"targetId": target, "type": kind, "url": url, "attached": False}
        if opener is not None:
            info |= {"openerId": opener, "canAccessOpener": reference}
        await self.peer.emit("Target.targetCreated", {"targetInfo": info})
        session = self._next("G")
        self.target_of[session], self.guard_of[target] = target, session
        await self.peer.emit(
            "Target.attachedToTarget",
            {"sessionId": session, "targetInfo": {**info, "attached": True}, "waitingForDebugger": True},
        )
        return session

    def sent(self, method: str, session: str | None = None) -> list[dict[str, Any]]:
        return [
            m
            for m in self.peer.received
            if m.get("method") == method and (session is None or m.get("sessionId") == session)
        ]


@dataclass
class Rig:
    fake: FakeBrowser
    chrome: Chrome
    registry: FrameRegistry


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    registry = FrameRegistry(tmp_path / "home")
    for task, host in (("a", "a.test"), ("b", "b.test")):
        registry.create(FrameSpecIn(task_id=task, kind=FrameKind.MACHINE, allow_domains=[host]))
    fake = FakeBrowser()
    chrome = Chrome(
        tmp_path / "home",
        "/unused",
        f"http://chrome:{TOKEN}@127.0.0.1:{PROXY_PORT}",
        fence=RequestFence(registry.frames),
    )
    await chrome.connect(*await fake.peer.listen())
    yield Rig(fake, chrome, registry)
    await chrome.close()
    await fake.peer.aclose()


async def until(check: Any, what: str) -> Any:
    for _ in range(200):
        if found := check():
            return found
        await asyncio.sleep(0.01)
    raise AssertionError(f"never happened: {what}")


class Agent:
    """One task's agent on its relay: what it is sent, and its calls answered by id."""

    def __init__(self, chrome: Chrome, ref: str) -> None:
        self.got: list[dict[str, Any]] = []
        self.relay = TaskRelay(chrome, ref, self.got.append)
        self._id = 0

    async def ask(self, method: str, params: dict[str, Any] | None = None, session: str | None = None):
        self._id += 1
        msg: dict[str, Any] = {"id": self._id, "method": method, "params": params or {}}
        if session is not None:
            msg["sessionId"] = session
        self.relay.handle(msg)
        wanted = self._id
        return await until(lambda: next((m for m in self.got if m.get("id") == wanted), None), method)

    def events(self, method: str) -> list[dict[str, Any]]:
        return [m for m in self.got if m.get("method") == method]


LOADER = "L1"


def replies(rig: Rig, request_id: str) -> list[dict[str, Any]]:
    return [m for m in rig.fake.peer.received if m.get("params", {}).get("requestId") == request_id]


async def image(
    rig: Rig,
    session: str,
    page: str,
    request_id: str,
    referrer: str | None,
    *,
    announced_on: str | None = None,
    loader: str | None = LOADER,
    frame: str | None = None,
    network: bool = True,
    kind: str = "Image",
) -> None:
    """An asset as Chrome shows it: announced by the session that asked, with the document's frame
    and loader, then paused on `session`. A dedicated worker's is paused on its page's session."""
    frame = frame or page
    if loader is not None:
        announcement = {"requestId": f"net-{request_id}", "frameId": frame, "loaderId": loader}
        await rig.fake.peer.emit("Network.requestWillBeSent", announcement, session=announced_on or session)
    headers = {"Referer": referrer} if referrer else {}
    request = {"url": "https://cdn.test/x.png", "headers": headers}
    params = {"requestId": request_id, "frameId": frame, "resourceType": kind, "request": request}
    if network:
        params["networkId"] = f"net-{request_id}"
    await rig.fake.peer.emit("Fetch.requestPaused", params, session=session)


async def commit(
    rig: Rig, session: str, url: str, loader: str = LOADER, *, frame: str = "f", parent: str | None = None
) -> None:
    said: dict[str, Any] = {"id": frame, "url": url, "loaderId": loader}
    if parent is not None:
        said["parentId"] = parent
    await rig.fake.peer.emit("Page.frameNavigated", {"frame": said}, session=session)


@dataclass
class Listed:
    """A task's page, committed to a listed URL, under a fence that tells what it excused."""

    rig: Rig
    page: str
    session: str
    told: list[tuple[str, int]]


@pytest.fixture
async def listed(rig: Rig) -> Listed:
    told: list[tuple[str, int]] = []
    rig.chrome._fence = RequestFence(rig.registry.frames, on_asset=lambda h, p: told.append((h, p)))  # pyright: ignore[reportPrivateUsage]
    page = await rig.chrome.window_for(A)
    session = rig.chrome.fence_session(page)
    await commit(rig, session, "https://a.test/")
    await until(lambda: rig.chrome.top_of(page), "the commit")
    return Listed(rig, page, session, told)


async def attach_child(listed: Listed, session: str, kind: str) -> str:
    target = f"T-{session}"
    info = {"targetId": target, "type": kind, "url": "blob:https://a.test/x", "attached": True}
    params = {"sessionId": session, "targetInfo": info, "waitingForDebugger": True}
    await listed.rig.fake.peer.emit("Target.attachedToTarget", params, session=listed.session)
    await until(lambda: listed.rig.fake.sent("Runtime.runIfWaitingForDebugger", session), "the child runs")
    return target


async def relay_said(listed: Listed, request_id: str) -> str:
    return (await until(lambda: replies(listed.rig, request_id), f"{request_id} answered"))[0]["method"]


async def requested(
    rig: Rig, session: str, request_id: str, frame: str, url: str, kind: str = "Document"
) -> None:
    params = {"requestId": request_id, "frameId": frame, "resourceType": kind, "request": {"url": url}}
    await rig.fake.peer.emit("Fetch.requestPaused", params, session=session)


async def goes(listed: Listed, request_id: str, url: str, kind: str = "Document") -> None:
    """A request of the page's main frame, no document needed to announce it."""
    await requested(listed.rig, listed.session, request_id, listed.page, url, kind)


async def second_page(
    listed: Listed, page: str, url: str, *, opener: str | None = None, reference: bool = True
) -> str:
    """Another page of the task, opened by the first or by `opener`, committed to `url`: its session."""
    rig = listed.rig
    rig.fake.window_of[page] = rig.fake.window_of[listed.page]
    session = await rig.fake.announce(page, opener=opener or listed.page, url="", reference=reference)
    await until(lambda: page in rig.chrome.fenced_pages(), "the second page is fenced")
    await commit(rig, session, url, f"loader-{page}", frame=page)
    await until(lambda: rig.chrome.top_of(page), "its commit")
    return session


async def on_off_list(listed: Listed, url: str = "https://www.amazon.test/ap/oa") -> None:
    """The person has gone off the list, and the page has landed there."""
    await goes(listed, "off", url)
    await until(lambda: replies(listed.rig, "off"), "the fence's answer")
    await commit(listed.rig, listed.session, url, "off")
    await until(lambda: listed.rig.chrome.top_of(listed.page) == url, "the page, off the list")


async def navigated(rig: Rig, session: str) -> str:
    """Where the helper sent this page on its own session."""
    return (await until(lambda: rig.fake.sent("Page.navigate", session), "the page sent back"))[-1]["params"][
        "url"
    ]
