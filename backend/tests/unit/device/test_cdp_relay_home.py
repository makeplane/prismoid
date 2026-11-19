# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A hand-off's pass ends with a page on its way home: what it holds until
it lands is the signed-in page, so the agent's read waits, and what never lands goes back. And the
way back itself, which a `Page.navigate` that answers has not proved. The rig is
`tests/support/rigs/relay_rig.py`; the real Chrome's half is `tests/integration/device/test_device_chrome_home.py`."""

from __future__ import annotations

import asyncio

import pytest

from prismoid.device import chrome_pages
from tests.support.rigs.relay_rig import (
    STAYS,
    A,
    Agent,
    Listed,
    commit,
    goes,
    navigated,
    on_off_list,
    relay_said,
    until,
)
from tests.support.rigs.relay_rig import listed as listed
from tests.support.rigs.relay_rig import rig as rig

CALLBACK = "https://a.test/callback"


async def _on_its_way_home(listed: Listed, net: str = "hop") -> Agent:
    """The person is off the list, on a navigation to a listed site that has not answered, and the
    agent's first command is in."""
    rig = listed.rig
    rig.chrome.hand_over(A)
    await on_off_list(listed)
    params = {
        "requestId": "hop",
        "frameId": listed.page,
        "resourceType": "Document",
        "networkId": net,
        "request": {"url": CALLBACK},
    }
    await rig.fake.peer.emit("Fetch.requestPaused", params, session=listed.session)
    assert await relay_said(listed, "hop") == "Fetch.continueRequest"
    agent = Agent(rig.chrome, A)
    agent.relay.handle({"id": 1, "method": "Target.getTargets", "params": {}})
    await asyncio.sleep(0.02)
    return agent


async def test_a_redirect_to_a_listed_site_under_way_is_not_interrupted(listed: Listed) -> None:
    rig = listed.rig
    agent = await _on_its_way_home(listed)
    assert not agent.got, "until it lands the tab holds the off-list page"
    assert not rig.fake.sent("Page.navigate")
    await commit(rig, listed.session, CALLBACK, "home")
    await until(lambda: agent.got, "the read, once it has landed")
    assert not rig.fake.sent("Page.navigate"), "a sign-in that comes home keeps its page"
    assert rig.chrome.top_of(listed.page) == CALLBACK


async def test_a_navigation_chrome_cancels_goes_back(listed: Listed) -> None:
    """A 204, a `window.stop()`, Esc: the off-list page never left."""
    rig = listed.rig
    agent = await _on_its_way_home(listed)
    params = {"requestId": "hop", "canceled": True}
    await rig.fake.peer.emit("Network.loadingFailed", params, session=listed.session)
    assert await navigated(rig, listed.session) == "https://a.test/"
    await until(lambda: agent.got, "the read, once it is back")
    assert rig.chrome.top_of(listed.page) == "https://a.test/"


async def test_a_navigation_past_the_bound_goes_back(listed: Listed, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chrome_pages, "_HOME_S", 0.02)
    rig = listed.rig
    agent = await _on_its_way_home(listed)
    assert await navigated(rig, listed.session) == "https://a.test/"
    await until(lambda: agent.got, "the read, once it is back")


async def test_a_navigation_that_falls_to_an_error_page_goes_back_at_once(listed: Listed) -> None:
    rig = listed.rig
    agent = await _on_its_way_home(listed)
    error = {"id": listed.page, "url": "chrome-error://chromewebdata/", "unreachableUrl": f"{CALLBACK}#x"}
    await rig.fake.peer.emit("Page.frameNavigated", {"frame": error}, session=listed.session)
    assert await navigated(rig, listed.session) == "https://a.test/"
    await until(lambda: agent.got, "the read, once it is back")


async def test_a_listed_page_heading_for_a_listed_page_is_nothing_to_wait_for(listed: Listed) -> None:
    rig = listed.rig
    rig.chrome.hand_over(A)
    await goes(listed, "next", "https://a.test/next")
    assert await relay_said(listed, "next") == "Fetch.continueRequest"
    rig.chrome.take_back(A)
    assert rig.chrome.going_back(A) is None and not rig.fake.sent("Page.navigate")


async def test_an_error_text_is_no_way_back(listed: Listed) -> None:
    """The last listed page answers 204 now: `Page.navigate` replies, and the page has not left."""
    rig = listed.rig
    rig.fake.navigation["https://a.test/"] = "net::ERR_ABORTED"
    rig.chrome.hand_over(A)
    await on_off_list(listed)
    agent = Agent(rig.chrome, A)
    await agent.ask("Target.getTargets")
    assert [m["params"]["url"] for m in rig.fake.sent("Page.navigate")] == ["https://a.test/", "about:blank"]
    assert rig.chrome.top_of(listed.page) == "about:blank"
    assert not rig.fake.sent("Target.closeTarget")


async def test_an_unchanged_top_is_no_way_back(listed: Listed, monkeypatch: pytest.MonkeyPatch) -> None:
    """It answered with no error, and nothing committed: the read must not find the page."""
    monkeypatch.setattr(chrome_pages, "_COMMIT_S", 0.02)
    rig = listed.rig
    rig.fake.navigation["https://a.test/"] = STAYS
    rig.chrome.hand_over(A)
    await on_off_list(listed)
    agent = Agent(rig.chrome, A)
    await agent.ask("Target.getTargets")
    assert rig.chrome.top_of(listed.page) == "about:blank"


async def test_a_page_that_refuses_about_blank_too_is_closed(
    listed: Listed, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chrome_pages, "_COMMIT_S", 0.02)
    rig = listed.rig
    rig.fake.navigation |= {"https://a.test/": "net::ERR_ABORTED", "about:blank": STAYS}
    rig.chrome.hand_over(A)
    await on_off_list(listed)
    agent = Agent(rig.chrome, A)
    await agent.ask("Target.getTargets")
    assert rig.fake.sent("Target.closeTarget")[0]["params"]["targetId"] == listed.page


async def test_about_blank_is_believed_after_a_way_back_that_failed(listed: Listed) -> None:
    """The first try left a navigation on the books; a commit it does not explain would be doubted."""
    rig = listed.rig
    rig.chrome.hand_over(A)
    await on_off_list(listed)
    rig.fake.navigation["https://a.test/"] = "net::ERR_FAILED"
    params = {
        "requestId": "n",
        "frameId": listed.page,
        "resourceType": "Document",
        "request": {"url": "https://a.test/"},
    }
    rig.chrome.take_back(A)
    await rig.fake.peer.emit("Fetch.requestPaused", params, session=listed.session)
    await until(lambda: rig.chrome.top_of(listed.page) == "about:blank", "the page, emptied")
    assert listed.page not in rig.chrome.navigating()
