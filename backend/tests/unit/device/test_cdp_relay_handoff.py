# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Prismoid Chrome fence's hand-off: the pages a person drives, and
what is and is not carried along. What a command wrote is `tests/unit/device/test_cdp_relay_written.py`.
The rig is `tests/support/rigs/relay_rig.py`."""

from __future__ import annotations

import pytest

from prismoid.device import chrome_pages
from tests.support.rigs.relay_rig import (
    A,
    Agent,
    B,
    Listed,
    attach_child,
    commit,
    goes,
    image,
    on_off_list,
    relay_said,
    requested,
    second_page,
    until,
)
from tests.support.rigs.relay_rig import listed as listed
from tests.support.rigs.relay_rig import rig as rig


async def test_a_person_in_a_hand_off_goes_where_the_sign_ingoes(listed: Listed) -> None:
    """Sign in with Amazon: the sign-in left the listed page and every list refused it."""
    await goes(listed, "before", "https://www.amazon.test/ap/oa", kind="XHR")
    assert await relay_said(listed, "before") == "Fetch.failRequest"

    listed.rig.chrome.hand_over(A)
    await goes(listed, "during", "https://www.amazon.test/ap/oa")
    assert await relay_said(listed, "during") == "Fetch.continueRequest"
    assert listed.told == [("www.amazon.test", 443)], "the proxy is told, and only for that host"
    assert "www.amazon.test" not in listed.rig.registry.frames[A].allow, "no list widens"


async def test_the_pages_it_reaches_work_and_their_own_requests_pass(listed: Listed) -> None:
    rig = listed.rig
    rig.chrome.hand_over(A)
    await goes(listed, "there", "https://www.amazon.test/ap/oa")
    assert await relay_said(listed, "there") == "Fetch.continueRequest"
    await commit(rig, listed.session, "https://www.amazon.test/ap/oa", "there")
    await until(lambda: rig.chrome.top_of(listed.page), "the commit")

    await image(rig, listed.session, listed.page, "xhr", None, loader="there", kind="XHR")
    assert await relay_said(listed, "xhr") == "Fetch.continueRequest"


async def test_the_agent_acting_ends_it(listed: Listed) -> None:
    rig = listed.rig
    rig.chrome.hand_over(A)
    agent = Agent(rig.chrome, A)
    agent.relay.handle({"id": 1, "method": "Page.screencastFrameAck", "params": {}, "sessionId": "x"})
    await goes(listed, "acked", "https://www.amazon.test/")
    assert await relay_said(listed, "acked") == "Fetch.continueRequest", "the recording is not the agent"

    await agent.ask("Target.getTargets")
    await goes(listed, "after", "https://www.amazon.test/")
    assert await relay_said(listed, "after") == "Fetch.failRequest"


async def test_a_page_on_loopback_is_never_handed(listed: Listed) -> None:
    """A fenced command writes that page: it could carry the command's data anywhere."""
    rig = listed.rig
    await commit(rig, listed.session, "http://127.0.0.1:3000/own.html", "own")
    await until(lambda: "127.0.0.1" in (rig.chrome.top_of(listed.page) or ""), "the commit")
    rig.chrome.hand_over(A)
    await goes(listed, "leak", "https://evil.test/?data=1")
    assert await relay_said(listed, "leak") == "Fetch.failRequest"
    assert not listed.told


async def test_a_person_led_to_a_loopback_page_loses_the_pass_with_it(listed: Listed) -> None:
    rig = listed.rig
    rig.chrome.hand_over(A)
    await commit(rig, listed.session, "http://localhost:3000/own.html", "own")
    await until(lambda: "localhost" in (rig.chrome.top_of(listed.page) or ""), "the commit")
    await goes(listed, "leak", "https://evil.test/?data=1")
    assert await relay_said(listed, "leak") == "Fetch.failRequest"


async def test_a_page_a_command_landed_on_after_an_error_has_no_way_back_to_be_handed(listed: Listed) -> None:
    """The person's early click left an error page. A commit that no navigation explains then
    lands on a page Prismoid cannot name: the way back it remembered is gone with the error page."""
    rig = listed.rig
    await commit(rig, listed.session, "https://a.test/first", "first")
    await until(lambda: rig.chrome.top_of(listed.page), "the listed page")
    await goes(listed, "refused", "https://b.test/early")
    assert await relay_said(listed, "refused") == "Fetch.failRequest"
    error = {
        "id": listed.page,
        "url": "chrome-error://chromewebdata/",
        "unreachableUrl": "https://b.test/early",
    }
    await rig.fake.peer.emit("Page.frameNavigated", {"frame": error}, session=listed.session)
    fell = rig.chrome._fell_from  # pyright: ignore[reportPrivateUsage]
    await until(lambda: fell.get(listed.page), "the way back")

    command = {"id": listed.page, "url": "https://c.test/unnamed", "loaderId": "unnamed"}
    await rig.fake.peer.emit("Page.frameNavigated", {"frame": command}, session=listed.session)
    await until(lambda: listed.page not in fell, "the way back gone")
    rig.chrome.hand_over(A)
    assert not rig.chrome.handed(A)


async def test_a_loopback_commit_nothing_explains_still_loses_the_pass(listed: Listed) -> None:
    """An older navigation landing late: the newest is elsewhere, and that page is still running."""
    rig = listed.rig
    rig.chrome.hand_over(A)
    await goes(listed, "newest", "https://a.test/next")
    assert await relay_said(listed, "newest") == "Fetch.continueRequest"
    await commit(rig, listed.session, "http://127.0.0.1:3000/own.html", "own")
    await goes(listed, "leak", "https://evil.test/?data=1")
    assert await relay_said(listed, "leak") == "Fetch.failRequest"


async def test_a_loopback_frame_cannot_take_its_top_or_a_popup_along(listed: Listed) -> None:
    """With a person's click on it, a frame may navigate its top, and open a popup."""
    rig = listed.rig
    rig.chrome.hand_over(A)
    await commit(rig, listed.session, "http://127.0.0.1:3000/f", "child", frame="kid", parent=listed.page)
    await until(lambda: "kid" in rig.chrome.documents_of(listed.page), "the frame")
    await goes(listed, "leak", "https://evil.test/?data=1")
    assert await relay_said(listed, "leak") == "Fetch.failRequest"

    rig.fake.window_of["P3"] = rig.fake.window_of[listed.page]
    popup = await rig.fake.announce("P3", opener=listed.page, url="")
    await until(lambda: "P3" in rig.chrome.fenced_pages(), "the popup is fenced")
    await requested(rig, popup, "gone", "P3", "https://evil.test/?data=2")
    assert await relay_said(listed, "gone") == "Fetch.failRequest"


async def test_a_frame_of_a_loopback_page_is_not_carried(listed: Listed) -> None:
    rig = listed.rig
    rig.chrome.hand_over(A)
    await commit(rig, listed.session, "http://127.0.0.1:3000/f", "child", frame="kid", parent=listed.page)
    await commit(rig, listed.session, "about:srcdoc", "grand", frame="deep", parent="kid")
    await image(rig, listed.session, listed.page, "leak", None, loader="grand", frame="deep", kind="XHR")
    assert await relay_said(listed, "leak") == "Fetch.failRequest"


async def _framed(listed: Listed, request_id: str, frame: str, url: str, parent: str | None = None) -> None:
    """An iframe's first request, as Chrome shows it: attached under its parent, then announced with
    the loader of a document that is not born until this request is let go."""
    rig = listed.rig
    if parent is not None:
        attached = {"frameId": frame, "parentFrameId": parent}
        await rig.fake.peer.emit("Page.frameAttached", attached, session=listed.session)
    announced = {"requestId": f"net-{request_id}", "frameId": frame, "loaderId": f"unborn-{request_id}"}
    await rig.fake.peer.emit("Network.requestWillBeSent", announced, session=listed.session)
    params = {
        "requestId": request_id,
        "frameId": frame,
        "resourceType": "Document",
        "networkId": f"net-{request_id}",
        "request": {"url": url},
    }
    await rig.fake.peer.emit("Fetch.requestPaused", params, session=listed.session)


async def test_an_iframe_of_a_handed_page_is_carried_at_once(
    listed: Listed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A captcha, or an identity host's frame. Its own document cannot be there yet, so waiting
    for it would be waiting on this very request."""
    monkeypatch.setattr(chrome_pages, "_COMMIT_S", 30.0)
    listed.rig.chrome.hand_over(A)
    await on_off_list(listed)
    await _framed(listed, "frame", "kid", "https://challenges.test/check", parent=listed.page)
    assert await relay_said(listed, "frame") == "Fetch.continueRequest"


async def test_a_frame_in_a_frame_is_judged_down_its_whole_chain(listed: Listed) -> None:
    rig = listed.rig
    rig.chrome.hand_over(A)
    await on_off_list(listed)
    await commit(rig, listed.session, "https://id.test/frame", "kid", frame="kid", parent=listed.page)
    await until(lambda: "kid" in rig.chrome.documents_of(listed.page), "the frame")
    await _framed(listed, "deep", "deep", "https://session.test/", parent="kid")
    assert await relay_said(listed, "deep") == "Fetch.continueRequest"


async def test_an_iframe_under_a_loopback_frame_is_not_carried(listed: Listed) -> None:
    rig = listed.rig
    rig.chrome.hand_over(A)
    await commit(rig, listed.session, "http://127.0.0.1:3000/f", "kid", frame="kid", parent=listed.page)
    await until(lambda: "kid" in rig.chrome.documents_of(listed.page), "the frame")
    await _framed(listed, "deep", "deep", "https://session.test/", parent="kid")
    assert await relay_said(listed, "deep") == "Fetch.failRequest"


async def test_an_iframe_is_not_carried_outside_a_pass(listed: Listed) -> None:
    await _framed(listed, "frame", "kid", "https://challenges.test/check", parent=listed.page)
    assert await relay_said(listed, "frame") == "Fetch.failRequest"


async def test_an_iframe_with_no_parent_in_the_page_is_refused(
    listed: Listed, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chrome_pages, "_COMMIT_S", 0.05)
    listed.rig.chrome.hand_over(A)
    await _framed(listed, "stray", "kid", "https://challenges.test/check")
    assert await relay_said(listed, "stray") == "Fetch.failRequest"


async def test_an_iframe_of_a_page_mid_swap_is_refused(
    listed: Listed, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chrome_pages, "_COMMIT_S", 0.05)
    listed.rig.chrome.hand_over(A)
    await goes(listed, "leaving", "https://a.test/next")
    await relay_said(listed, "leaving")
    await _framed(listed, "late", "kid", "https://challenges.test/check", parent=listed.page)
    assert await relay_said(listed, "late") == "Fetch.failRequest"


async def test_an_iframe_of_a_worker_is_no_such_thing(listed: Listed) -> None:
    """A worker's session has no document, so nothing it asks for is carried, of any type."""
    rig = listed.rig
    rig.chrome.hand_over(A)
    worker = "S-worker"
    await attach_child(listed, worker, "worker")
    await image(
        rig, listed.session, listed.page, "leak", None, announced_on=worker, kind="Document", frame="kid"
    )
    assert await relay_said(listed, "leak") == "Fetch.failRequest"


async def test_a_worker_is_not_carried(listed: Listed) -> None:
    rig = listed.rig
    rig.chrome.hand_over(A)
    worker = "S-worker"
    await attach_child(listed, worker, "worker")
    await image(rig, listed.session, listed.page, "leak", None, announced_on=worker, kind="XHR")
    assert await relay_said(listed, "leak") == "Fetch.failRequest"


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:9/", "http://[::1]:9/", "http://10.0.0.1/", "https://me:pw@evil.test/"]
)
async def test_nowhere_private_and_no_credential_in_a_url(listed: Listed, url: str) -> None:
    listed.rig.chrome.hand_over(A)
    await goes(listed, "no", url)
    assert await relay_said(listed, "no") == "Fetch.failRequest"
    assert not listed.told


async def test_a_sign_ins_popup_is_carried_and_a_tab_with_no_opener_is_not(listed: Listed) -> None:
    rig = listed.rig
    rig.chrome.hand_over(A)
    rig.fake.window_of["P1"] = rig.fake.window_of[listed.page]
    popup = await rig.fake.announce("P1", opener=listed.page, url="")
    await until(lambda: "P1" in rig.chrome.fenced_pages(), "the popup is fenced")
    session = rig.chrome.fence_session("P1")
    assert popup == session
    await requested(rig, session, "popup", "P1", "https://www.amazon.test/ap/oa")
    assert await relay_said(listed, "popup") == "Fetch.continueRequest"

    tab = await rig.fake.announce("P2", window=rig.fake.window_of[listed.page], url="")
    await until(lambda: "P2" in rig.chrome.fenced_pages(), "the tab is fenced")
    await requested(rig, tab, "tab", "P2", "https://evil.test/?data=1")
    assert await relay_said(listed, "tab") == "Fetch.failRequest"


async def test_the_other_tasks_pages_are_not_carried(listed: Listed) -> None:
    rig = listed.rig
    other = await rig.chrome.window_for(B)
    session = rig.chrome.fence_session(other)
    await commit(rig, session, "https://b.test/")
    rig.chrome.hand_over(A)
    await requested(rig, session, "b", other, "https://www.amazon.test/")
    assert await relay_said(listed, "b") == "Fetch.failRequest"


COMMANDS_PAGES = [
    "http://127.0.0.1:3000/own.html",
    "data:text/html,<script>opener.location='https://evil.test/'</script>",
    "file:///Users/me/out/own.html",
    "blob:http://127.0.0.1:3000/3f0c",
]


@pytest.mark.parametrize("url", COMMANDS_PAGES)
async def test_a_listed_page_beside_a_commands_page_is_not_handed(listed: Listed, url: str) -> None:
    """A page that opened another holds a window reference, and steers it with no person there."""
    rig = listed.rig
    popup = await second_page(listed, "P4", "https://a.test/page")
    await commit(rig, listed.session, url, "own")
    await until(lambda: rig.chrome.top_of(listed.page) == url, "the command's page")
    rig.chrome.hand_over(A)
    await requested(rig, popup, "steered", "P4", "https://evil.test/?data=1")
    assert await relay_said(listed, "steered") == "Fetch.failRequest"


async def test_a_commands_page_leaving_after_the_hand_off_gives_no_pass_back(listed: Listed) -> None:
    rig = listed.rig
    popup = await second_page(listed, "P5", "https://a.test/page")
    await commit(rig, listed.session, "http://127.0.0.1:3000/own.html", "own")
    await until(lambda: rig.chrome.top_of(listed.page), "the command's page")
    rig.chrome.hand_over(A)
    await commit(rig, listed.session, "https://a.test/page", "gone")
    await until(lambda: "a.test" in (rig.chrome.top_of(listed.page) or ""), "it left")
    await requested(rig, popup, "after", "P5", "https://evil.test/?data=1")
    assert await relay_said(listed, "after") == "Fetch.failRequest"


async def test_a_commands_page_landing_anywhere_in_the_task_ends_the_pass(listed: Listed) -> None:
    rig = listed.rig
    popup = await second_page(listed, "P6", "https://a.test/page")
    rig.chrome.hand_over(A)
    await requested(rig, popup, "carried", "P6", "https://a.test/ap/oa")
    assert await relay_said(listed, "carried") == "Fetch.continueRequest"
    await commit(rig, listed.session, "http://127.0.0.1:3000/own.html", "own")
    await until(lambda: (rig.chrome.top_of(listed.page) or "").startswith("http://127"), "the command's page")
    await commit(rig, listed.session, "https://a.test/page", "gone")
    await until(lambda: "a.test" in (rig.chrome.top_of(listed.page) or ""), "it left")
    await requested(rig, popup, "after", "P6", "https://evil.test/?data=1")
    assert await relay_said(listed, "after") == "Fetch.failRequest"
