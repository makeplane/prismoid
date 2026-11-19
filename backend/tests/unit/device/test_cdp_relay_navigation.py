# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Prismoid Chrome fence's navigations: a page's top URL known only
from its commit, a navigation Chrome cancelled, and a frame going. The rig is `tests/support/rigs/relay_rig.py`."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from prismoid.device.chrome import Chrome
from prismoid.device.egress import SEED_URLS
from tests.support.rigs.relay_rig import A, Agent, B, Rig, commit, until
from tests.support.rigs.relay_rig import rig as rig


async def test_a_pages_top_url_is_known_only_from_its_commit(rig: Rig) -> None:
    """From a main-frame Document request until that navigation commits, the page has no URL: the
    old one must not excuse a subresource of the new one."""
    page = await rig.chrome.window_for(A)
    session = rig.chrome.fence_session(page)
    chrome = rig.chrome

    def document(url: str) -> dict[str, Any]:
        return {"requestId": "d", "frameId": page, "resourceType": "Document", "request": {"url": url}}

    assert chrome.top_of(page) is None, "a page never seen to commit is unknown"
    await commit(rig, session, "https://a.test/")
    await until(lambda: chrome.top_of(page) == "https://a.test/", "the commit")

    rig.chrome._on_paused(session, document("http://localhost:3000/"))  # pyright: ignore[reportPrivateUsage]
    assert chrome.top_of(page) is None, "mid-swap: the old page's URL is gone"

    await commit(rig, session, "https://a.test/older")  # an older navigation, landing late
    await asyncio.sleep(0.05)
    assert chrome.top_of(page) is None, "a commit the newest navigation does not explain says nothing"

    await commit(rig, session, "http://localhost:3000/")
    await until(lambda: chrome.top_of(page) == "http://localhost:3000/", "the commit")

    rig.chrome._on_paused(session, {**document("https://a.test/x"), "frameId": "an-iframe"})  # pyright: ignore[reportPrivateUsage]
    assert chrome.top_of(page) == "http://localhost:3000/", "an iframe's document moves nothing"


def test_chrome_is_told_its_two_doors_by_scheme(tmp_path: Path) -> None:
    """`socks` is how Chrome's proxy rules name a WebSocket's proxy: it may be an http one."""
    chrome = Chrome(tmp_path, "/unused", "http://chrome:t@127.0.0.1:1", socket_proxy="http://127.0.0.1:2")
    assert chrome._proxy_rules() == "http=127.0.0.1:2;https=127.0.0.1:1;socks=http://127.0.0.1:2"  # pyright: ignore[reportPrivateUsage]
    alone = Chrome(tmp_path, "/unused", "http://chrome:t@127.0.0.1:1")
    assert alone._proxy_rules() == "http://127.0.0.1:1"  # pyright: ignore[reportPrivateUsage]


async def test_each_door_is_seeded_once_from_the_first_window(rig: Rig) -> None:
    """A WebSocket's 407 and a cross-origin image's reach no event, so one plain request to each door
    puts the credential in Chrome's cache first, in a world no page can reach."""
    rig.chrome._socket_proxy = "http://127.0.0.1:2"  # pyright: ignore[reportPrivateUsage]
    page = await rig.chrome.window_for(A)
    await rig.chrome.window_for(B)

    [world] = rig.fake.sent("Page.createIsolatedWorld")
    assert world["params"] == {"frameId": page, "worldName": "prismoid"}
    [seed] = rig.fake.sent("Runtime.evaluate")
    assert seed["sessionId"] == rig.chrome.fence_session(page)
    assert seed["params"]["contextId"] == 7 and seed["params"]["awaitPromise"] is True
    assert all(url in seed["params"]["expression"] for url in SEED_URLS)


async def test_one_door_has_nothing_to_seed(rig: Rig) -> None:
    await rig.chrome.window_for(A)
    assert not rig.fake.sent("Page.createIsolatedWorld") and not rig.fake.sent("Runtime.evaluate")


async def test_chrome_gone_ends_every_relay(rig: Rig) -> None:
    await rig.chrome.window_for(A)
    a = Agent(rig.chrome, A)
    await rig.fake.peer.drop()
    await until(lambda: a.relay.lost.is_set(), "the relay learns Chrome is gone")
    assert not rig.chrome.running


def _document(page: str, url: str, network: str) -> dict[str, Any]:
    return {
        "requestId": "d",
        "frameId": page,
        "resourceType": "Document",
        "request": {"url": url},
        "networkId": network,
    }


async def _on(rig: Rig, url: str) -> tuple[str, str]:
    """A's page, committed on `url`: (the page, its fence session)."""
    page = await rig.chrome.window_for(A)
    session = rig.chrome.fence_session(page)
    await commit(rig, session, url)
    await until(lambda: rig.chrome.top_of(page) == url, "the commit")
    return page, session


async def _failed(rig: Rig, session: str | None, request: str, *, canceled: bool = True) -> None:
    await rig.fake.peer.emit(
        "Network.loadingFailed", {"requestId": request, "canceled": canceled}, session=session
    )
    await rig.fake.peer.emit("Page.loadEventFired", {}, session=session)
    await asyncio.sleep(0.02)


async def test_a_navigation_chrome_cancelled_gives_the_page_its_committed_url_back(rig: Rig) -> None:
    page, session = await _on(rig, "https://a.test/")
    chrome = rig.chrome
    chrome._on_paused(session, _document(page, "https://a.test/204", "N1"))  # pyright: ignore[reportPrivateUsage]
    chrome._on_paused(None, _document(page, "https://a.test/204", "N1"))  # pyright: ignore[reportPrivateUsage]
    assert chrome.top_of(page) is None, "mid-swap"
    await _failed(rig, session, "N1")
    assert chrome.top_of(page) == "https://a.test/" and page not in chrome.navigating()
    assert chrome.landed(page), "what waited for a commit hears there is none"


async def test_only_the_newest_navigation_cancelled_says_anything(rig: Rig) -> None:
    page, session = await _on(rig, "https://a.test/")
    chrome = rig.chrome
    chrome._on_paused(session, _document(page, "https://a.test/one", "N1"))  # pyright: ignore[reportPrivateUsage]
    chrome._on_paused(session, _document(page, "https://a.test/two", "N2"))  # pyright: ignore[reportPrivateUsage]
    await _failed(rig, session, "N1")
    assert chrome.top_of(page) is None, "the second is still on its way"
    await _failed(rig, session, "N2")
    assert chrome.top_of(page) == "https://a.test/", "what it last committed, through both"


async def test_a_request_cancelled_that_is_no_navigation_moves_nothing(rig: Rig) -> None:
    page, session = await _on(rig, "https://a.test/")
    chrome = rig.chrome
    chrome._on_paused(session, _document(page, "https://a.test/x", "N1"))  # pyright: ignore[reportPrivateUsage]
    await _failed(rig, session, "some-image")
    await _failed(rig, None, "N1")  # no session of the fence's
    chrome._page_of["an-iframes"] = page  # pyright: ignore[reportPrivateUsage]
    await _failed(rig, "an-iframes", "N1")  # its page's, and not the fence's
    assert chrome.top_of(page) is None


async def test_a_navigation_that_failed_waits_for_its_error_page(rig: Rig) -> None:
    """Refused or unreachable, it is no cancellation: Chrome commits an error page in its place, so
    the page is about to be on neither."""
    page, session = await _on(rig, "https://a.test/")
    chrome = rig.chrome
    chrome._on_paused(session, _document(page, "https://b.test/", "N1"))  # pyright: ignore[reportPrivateUsage]
    await _failed(rig, session, "N1", canceled=False)
    assert chrome.top_of(page) is None


async def test_a_commit_nothing_explains_leaves_nothing_to_go_back_to(rig: Rig) -> None:
    """The page moved on from the URL it had, to a place not believed: a blob, or a navigation older
    than the newest. So the page is on no URL anyone can vouch for, and a cancelled newest navigation
    must not hand it the old one: that was a listed URL, for a page that is not on it."""
    page, session = await _on(rig, "https://a.test/")
    chrome = rig.chrome
    chrome._on_paused(session, _document(page, "https://a.test/one", "N1"))  # pyright: ignore[reportPrivateUsage]
    await commit(rig, session, "http://localhost:3000/landed-late")
    await asyncio.sleep(0.02)
    chrome._on_paused(session, _document(page, "https://a.test/two", "N2"))  # pyright: ignore[reportPrivateUsage]
    await _failed(rig, session, "N2")
    assert chrome.top_of(page) is None


async def test_a_pause_with_no_network_id_is_never_given_up(rig: Rig) -> None:
    page, session = await _on(rig, "https://a.test/")
    chrome = rig.chrome
    chrome._on_paused(session, _document(page, "https://a.test/x", ""))  # pyright: ignore[reportPrivateUsage]
    await _failed(rig, session, "")
    assert chrome.top_of(page) is None


async def test_the_browser_session_takes_only_a_pages_document_for_a_navigation(rig: Rig) -> None:
    """It names no page, so every frame looks like its own. A same-process iframe's is no target."""
    page, _ = await _on(rig, "https://a.test/")
    chrome = rig.chrome
    chrome._on_paused(None, _document("an-iframe", "https://a.test/f", "N1"))  # pyright: ignore[reportPrivateUsage]
    assert not chrome.navigating() and "an-iframe" not in chrome._landed  # pyright: ignore[reportPrivateUsage]
    assert chrome.top_of(page) == "https://a.test/"
    chrome._on_paused(None, _document(page, "https://a.test/next", "N2"))  # pyright: ignore[reportPrivateUsage]
    assert page in chrome.navigating()


async def _iframe(rig: Rig, session: str, frame: str, parent: str, loader: str = "L") -> None:
    await rig.fake.peer.emit(
        "Page.frameNavigated",
        {"frame": {"id": frame, "parentId": parent, "url": "https://a.test/f", "loaderId": loader}},
        session=session,
    )


async def test_a_removed_frame_takes_its_children_and_a_swapped_one_stays(rig: Rig) -> None:
    page, session = await _on(rig, "https://a.test/")
    await _iframe(rig, session, "F1", page)
    await _iframe(rig, session, "F2", "F1")
    await _iframe(rig, session, "F3", page)
    await until(lambda: len(rig.chrome.documents_of(page)) == 4, "the frames")
    await rig.fake.peer.emit("Page.frameDetached", {"frameId": "F3", "reason": "swap"}, session=session)
    await rig.fake.peer.emit("Page.frameDetached", {"frameId": "F1", "reason": "remove"}, session=session)
    await until(lambda: "F1" not in rig.chrome.documents_of(page), "the frame")
    assert set(rig.chrome.documents_of(page)) == {page, "F3"}, (
        "its child went with it, and a swap is no removal"
    )
    await rig.fake.peer.emit("Page.frameDetached", {"frameId": page, "reason": "remove"}, session=session)
    await asyncio.sleep(0.02)
    assert page in rig.chrome.documents_of(page), "a page is its target's, whatever is said of its frame"


async def test_only_the_fences_sessions_are_heard_on_a_frame_going(rig: Rig) -> None:
    page, session = await _on(rig, "https://a.test/")
    await _iframe(rig, session, "F1", page)
    a = Agent(rig.chrome, A)
    own = (await a.ask("Target.attachToTarget", {"targetId": page, "flatten": True}))["result"]["sessionId"]
    await until(lambda: "F1" in rig.chrome.documents_of(page), "the frame")
    await rig.fake.peer.emit("Page.frameDetached", {"frameId": "F1", "reason": "remove"}, session=own)
    await asyncio.sleep(0.02)
    assert "F1" in rig.chrome.documents_of(page)
