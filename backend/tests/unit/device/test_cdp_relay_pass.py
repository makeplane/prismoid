# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A hand-off's pass ends: at a ceiling that sleep cannot stop, at whatever
says nobody is parked, and what it left off the list goes back before the agent reads it. The rig is
`tests/support/rigs/relay_rig.py`; the real Chrome's half is `tests/integration/device/test_device_chrome_pass.py`."""

from __future__ import annotations

import asyncio

import pytest

from prismoid.device import chrome_pages
from prismoid.device.chrome_pages import PASS_S
from tests.support.rigs.relay_rig import (
    A,
    Agent,
    Listed,
    commit,
    goes,
    navigated,
    on_off_list,
    relay_said,
    requested,
    second_page,
    until,
)
from tests.support.rigs.relay_rig import listed as listed
from tests.support.rigs.relay_rig import rig as rig


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(listed: Listed, monkeypatch: pytest.MonkeyPatch) -> Clock:
    made = Clock()
    monkeypatch.setattr(listed.rig.chrome, "clock", made)
    monkeypatch.setattr(chrome_pages, "_PASS_TICK_S", 0.01)
    return made


async def test_thirty_minutes_on_the_clock_is_the_ceiling(listed: Listed, clock: Clock) -> None:
    assert PASS_S == 1800
    listed.rig.chrome.hand_over(A)
    clock.now += PASS_S - 1
    await goes(listed, "last", "https://www.amazon.test/ap/oa")
    assert await relay_said(listed, "last") == "Fetch.continueRequest"
    clock.now += 1
    await goes(listed, "past", "https://www.amazon.test/ap/oa")
    assert await relay_said(listed, "past") == "Fetch.failRequest"


async def test_a_mac_asleep_past_the_ceiling_wakes_with_the_pass_over(listed: Listed, clock: Clock) -> None:
    """The wall clock jumps and no timer has fired: the first request is judged by the clock."""
    rig = listed.rig
    rig.chrome.hand_over(A)
    clock.now += 8 * 3600
    await goes(listed, "woke", "https://www.amazon.test/ap/oa")
    assert await relay_said(listed, "woke") == "Fetch.failRequest"
    await until(lambda: not rig.chrome.handed(A), "the pass closed with no one asking")


async def test_a_clock_set_back_is_the_end_of_the_pass_not_more_of_it(listed: Listed, clock: Clock) -> None:
    listed.rig.chrome.hand_over(A)
    clock.now -= 60
    await goes(listed, "back", "https://www.amazon.test/ap/oa")
    assert await relay_said(listed, "back") == "Fetch.failRequest"


async def test_a_second_hand_off_call_buys_no_more_time(listed: Listed, clock: Clock) -> None:
    listed.rig.chrome.hand_over(A)
    clock.now += PASS_S - 1
    listed.rig.chrome.hand_over(A)
    clock.now += 1
    await goes(listed, "past", "https://www.amazon.test/ap/oa")
    assert await relay_said(listed, "past") == "Fetch.failRequest"


async def test_a_second_hand_off_call_takes_no_page_away(listed: Listed) -> None:
    """Two calls in one turn, or a retried request, with the person already off the list."""
    rig = listed.rig
    rig.chrome.hand_over(A)
    await on_off_list(listed)
    rig.chrome.hand_over(A)
    assert listed.page in rig.chrome.handed(A)
    await goes(listed, "next", "https://www.amazon.test/next")
    assert await relay_said(listed, "next") == "Fetch.continueRequest"
    await commit(rig, listed.session, "https://www.amazon.test/next", "next")
    await until(lambda: "next" in (rig.chrome.top_of(listed.page) or ""), "its commit")
    rig.chrome.take_back(A)
    assert await navigated(rig, listed.session) == "https://a.test/"


async def test_the_end_of_a_pass_sends_back_a_page_it_never_held(listed: Listed) -> None:
    """The agent reads every page it drives, and a page off the list is off the list however it
    got there: here one mid-navigation at the hand-off, so never handed, under a list narrowed since."""
    rig = listed.rig
    popup = await second_page(listed, "P9", "https://a.test/page")
    await requested(rig, popup, "next", "P9", "https://a.test/next")
    assert await relay_said(listed, "next") == "Fetch.continueRequest"
    rig.chrome.hand_over(A)
    assert "P9" not in rig.chrome.handed(A)
    await commit(rig, popup, "https://a.test/next", "next", frame="P9")
    await until(lambda: "next" in (rig.chrome.top_of("P9") or ""), "its commit")
    rig.registry.frames[A].allow = {"pypi.org"}
    rig.chrome.take_back(A)
    assert await navigated(rig, popup) == "about:blank"


async def test_the_ceiling_leaves_the_window_usable_on_listed_sites(listed: Listed, clock: Clock) -> None:
    """The person may still be signing in: nothing is closed, and the list holds as it always did."""
    rig = listed.rig
    rig.chrome.hand_over(A)
    clock.now += PASS_S
    await until(lambda: not rig.chrome.handed(A), "the pass closed")
    await goes(listed, "home", "https://a.test/home")
    assert await relay_said(listed, "home") == "Fetch.continueRequest"
    assert rig.chrome.pages_of_task(A) == [listed.page]
    assert not rig.fake.sent("Page.navigate"), "a listed page stays where it is"


async def test_the_ceiling_sends_a_page_left_off_the_list_back(listed: Listed, clock: Clock) -> None:
    rig = listed.rig
    await commit(rig, listed.session, "https://a.test/signing-in", "in")
    await until(lambda: "signing-in" in (rig.chrome.top_of(listed.page) or ""), "the listed page")
    rig.chrome.hand_over(A)
    await on_off_list(listed)
    clock.now += PASS_S
    assert await navigated(rig, listed.session) == "https://a.test/signing-in"


async def test_the_agents_first_read_waits_for_the_page_to_go_back(listed: Listed) -> None:
    """The order is the point: a read that overtook the navigation would be of the signed-in page."""
    rig = listed.rig
    rig.chrome.hand_over(A)
    await on_off_list(listed)
    held = rig.fake.peer.holds["Page.navigate"] = asyncio.Event()
    agent = Agent(rig.chrome, A)
    agent.relay.handle({"id": 1, "method": "Target.getTargets", "params": {}})
    agent.relay.handle({"id": 2, "method": "Browser.getVersion", "params": {}})
    assert await navigated(rig, listed.session) == "https://a.test/"
    await asyncio.sleep(0.05)
    assert not agent.got, "nothing of the agent's goes on while the page is on its way"
    held.set()
    await until(lambda: len(agent.got) == 2, "both, once it has left")
    assert [m["id"] for m in agent.got] == [1, 2]
    assert rig.chrome.top_of(listed.page) == "https://a.test/", "and it did leave"


async def test_a_page_that_will_not_leave_is_closed(listed: Listed, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = listed.rig
    monkeypatch.setattr(chrome_pages, "_BACK_S", 0.05)
    rig.fake.peer.holds["Page.navigate"] = asyncio.Event()
    rig.chrome.hand_over(A)
    await on_off_list(listed)
    agent = Agent(rig.chrome, A)
    await agent.ask("Target.getTargets")
    assert rig.fake.sent("Target.closeTarget")[0]["params"]["targetId"] == listed.page


async def test_a_sign_in_that_ends_on_a_listed_site_stays_there(listed: Listed) -> None:
    rig = listed.rig
    rig.chrome.hand_over(A)
    await on_off_list(listed)
    await goes(listed, "home", "https://a.test/signed-in?code=1")
    assert await relay_said(listed, "home") == "Fetch.continueRequest"
    await commit(rig, listed.session, "https://a.test/signed-in?code=1", "home")
    await until(lambda: "signed-in" in (rig.chrome.top_of(listed.page) or ""), "the listed page")
    rig.chrome.take_back(A)
    assert not rig.fake.sent("Page.navigate")


async def test_a_navigation_under_way_to_an_off_list_site_goes_back(listed: Listed) -> None:
    rig = listed.rig
    rig.chrome.hand_over(A)
    await goes(listed, "off", "https://www.amazon.test/ap/oa")
    assert await relay_said(listed, "off") == "Fetch.continueRequest"
    rig.chrome.take_back(A)
    assert await navigated(rig, listed.session) == "https://a.test/"


async def test_a_page_never_on_the_list_goes_to_about_blank(listed: Listed) -> None:
    """A sign-in's popup: nowhere listed to go back to."""
    rig = listed.rig
    rig.chrome.hand_over(A)
    popup = await second_page(listed, "P1", "https://www.amazon.test/ap/oa")
    rig.chrome.take_back(A)
    assert await navigated(rig, popup) == "about:blank"


async def test_a_last_listed_page_since_narrowed_is_not_returned_to(listed: Listed) -> None:
    rig = listed.rig
    rig.chrome.hand_over(A)
    await on_off_list(listed)
    rig.registry.frames[A].allow = {"pypi.org"}
    rig.chrome.take_back(A)
    assert await navigated(rig, listed.session) == "about:blank"


async def test_a_blob_of_an_off_list_site_goes_back_too(listed: Listed) -> None:
    rig = listed.rig
    rig.chrome.hand_over(A)
    await commit(rig, listed.session, "blob:https://www.amazon.test/3f0c", "blob")
    await until(lambda: "blob" in (rig.chrome.top_of(listed.page) or ""), "the blob")
    rig.chrome.take_back(A)
    assert await navigated(rig, listed.session) == "https://a.test/"


async def test_a_commands_page_landing_sends_the_pages_back_too(listed: Listed) -> None:
    """The pass ending that way is a pass ending: the agent can read what it left all the same."""
    rig = listed.rig
    popup = await second_page(listed, "P2", "https://a.test/page")
    rig.chrome.hand_over(A)
    await on_off_list(listed)
    await commit(rig, popup, "http://127.0.0.1:3000/own.html", "own", frame="P2")
    assert await navigated(rig, listed.session) == "https://a.test/"
    assert not rig.fake.sent("Page.navigate", popup), "the task's own page stays where it is"


async def test_a_tab_with_no_opener_already_open_is_not_handed(listed: Listed) -> None:
    """One a page conjured with a synthetic Cmd-click: fenced for the window it is in, never driven."""
    rig = listed.rig
    tab = await rig.fake.announce("P3", window=rig.fake.window_of[listed.page], url="")
    await until(lambda: "P3" in rig.chrome.fenced_pages(), "the tab is fenced")
    await commit(rig, tab, "https://a.test/tab", "tab", frame="P3")
    await until(lambda: rig.chrome.top_of("P3"), "its commit")
    rig.chrome.hand_over(A)
    assert rig.chrome.handed(A) == {listed.page}
    await requested(rig, tab, "tab", "P3", "https://evil.test/?data=1")
    assert await relay_said(listed, "tab") == "Fetch.failRequest"


async def test_a_page_in_no_tasks_window_bypasses_workers_before_anything_waits(listed: Listed) -> None:
    """The bypass takes `Network` on, on every page, and a page's adoption can wait on a window."""
    rig = listed.rig
    base = await rig.fake.announce("P4", url="", window=999)
    await until(lambda: rig.fake.sent("Network.setBypassServiceWorker", base), "the bypass")
    await until(lambda: rig.fake.sent("Fetch.enable", base), "the interception")  # sent a moment later
    said = [m.get("method") for m in rig.fake.peer.received if m.get("sessionId") == base]
    assert said.index("Network.enable") < said.index("Network.setBypassServiceWorker")
    assert said.index("Network.setBypassServiceWorker") < said.index("Fetch.enable")


async def test_an_iframe_of_a_page_in_no_tasks_window_bypasses_them_too(listed: Listed) -> None:
    rig = listed.rig
    base = await rig.fake.announce("P5", url="", window=999)
    await until(lambda: rig.fake.sent("Runtime.runIfWaitingForDebugger", base), "the page runs")
    info = {"targetId": "F1", "type": "iframe", "url": "", "attached": True}
    params = {"sessionId": "S-frame", "targetInfo": info, "waitingForDebugger": True}
    await rig.fake.peer.emit("Target.attachedToTarget", params, session=base)
    await until(lambda: rig.fake.sent("Network.setBypassServiceWorker", "S-frame"), "the bypass")
    assert rig.fake.sent("Network.enable", "S-frame")


async def test_a_commit_no_fence_saw_off_the_list_is_taken_off_it(listed: Listed) -> None:
    """A worker in the profile answered a tab's first navigation: no request, so no refusal, but the
    page lands off its list."""
    rig = listed.rig
    tab = await rig.fake.announce("P6", window=rig.fake.window_of[listed.page], url="")
    await until(lambda: "P6" in rig.chrome.fenced_pages(), "the tab is fenced")
    await commit(rig, tab, "https://evil.test/cached", "cached", frame="P6")
    assert await navigated(rig, tab) == "about:blank"
    assert [d["host"] for d in rig.registry.frames[A].denials] == ["evil.test"]


async def test_an_error_page_nothing_explains_stays(listed: Listed) -> None:
    rig = listed.rig
    frame = {"id": listed.page, "url": "https://evil.test/", "unreachableUrl": "https://evil.test/"}
    await rig.fake.peer.emit("Page.frameNavigated", {"frame": frame}, session=listed.session)
    await commit(rig, listed.session, "https://a.test/after", "after")
    await until(lambda: "after" in (rig.chrome.top_of(listed.page) or ""), "the next commit")
    assert not rig.fake.sent("Page.navigate")


async def test_a_commit_in_a_pass_is_the_persons(listed: Listed) -> None:
    rig = listed.rig
    rig.chrome.hand_over(A)
    await commit(rig, listed.session, "https://www.amazon.test/ap/oa", "there")
    await until(lambda: "amazon" in (rig.chrome.top_of(listed.page) or ""), "the commit")
    assert not rig.fake.sent("Page.navigate")
