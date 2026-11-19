# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A hand-off's pass ends on the real Chrome: what the person left off the
list goes back before the agent reads it, and a sign-in that came home stays. Sites and the helper
fixture are `tests/support/rigs/chrome_rig.py`; the rules, on a scripted Chrome, `tests/unit/device/test_cdp_relay_pass.py`."""

from __future__ import annotations

import pytest

from prismoid.ai.frames.cdp_ws import WsCdp
from prismoid.device import chrome_pages
from prismoid.device.chrome_pages import PASS_S
from tests.support.markers import darwin_only, needs_chrome
from tests.support.rigs.chrome_rig import Helper, at, chrome_eval, chrome_page, hand_over, person_goes, until
from tests.support.rigs.chrome_rig import helper as helper


async def _on_listed_page(helper: Helper, ref: str, path: str = "/link-first") -> None:
    """The agent opened a listed page, and let go of its connection, as a park does."""
    cdp = await helper.dial(ref)
    _, session = await chrome_page(cdp)
    await at(cdp, session, helper, "a.test", path)
    await cdp.close()


async def _first_read(helper: Helper, ref: str) -> tuple[WsCdp, str, str]:
    """The agent comes back and reads the page at once: (its connection, its session, the URL)."""
    cdp = await helper.dial(ref)
    _, session = await chrome_page(cdp)
    return cdp, session, await chrome_eval(cdp, session, "location.href")


def _at(helper: Helper, ref: str, part: str) -> bool:
    return any(part in url for url in helper.chrome.tops_of(ref).values())


@darwin_only
@needs_chrome
async def test_the_agents_first_read_is_never_of_the_off_list_page(helper: Helper) -> None:
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    await _on_listed_page(helper, ref)
    await hand_over(helper, ref)
    await person_goes(helper, ref, f"http://b.test:{port}/signed-in")
    await until(lambda: _at(helper, ref, "b.test"), "the person, off the list")

    cdp, session, read = await _first_read(helper, ref)
    try:
        assert read == f"http://a.test:{port}/link-first"
        assert "a.test" in await chrome_eval(cdp, session, "document.title")
        await cdp.call("Page.navigate", {"url": f"http://b.test:{port}/again"}, session_id=session)
        await until(lambda: any(d["host"] == "b.test" for d in helper.chrome_denials(ref)), "the list again")
        assert not helper.site.saw("b.test", "/again")
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_a_slow_page_to_go_back_to_still_comes_before_the_read(helper: Helper) -> None:
    """The first read is held until the page has committed, not until it was told to go."""
    port = helper.site.port
    helper.site.slow["/link-slow"] = 0.8
    ref = await helper.frame("ta", "a.test")
    await _on_listed_page(helper, ref, "/link-slow")
    await hand_over(helper, ref)
    await person_goes(helper, ref, f"http://b.test:{port}/signed-in")
    await until(lambda: _at(helper, ref, "b.test"), "the person, off the list")
    cdp, session, read = await _first_read(helper, ref)
    try:
        assert read == f"http://a.test:{port}/link-slow"
        assert "a.test" in await chrome_eval(cdp, session, "document.title")
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_a_page_that_asks_the_person_to_stay_does_not_stay(
    helper: Helper, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its `beforeunload` holds the way back open with nobody there to answer: the page goes."""
    monkeypatch.setattr(chrome_pages, "_BACK_S", 1.0)
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    await _on_listed_page(helper, ref)
    await hand_over(helper, ref)
    chrome = helper.chrome
    page = chrome.pages_of_task(ref)[0]
    await person_goes(helper, ref, f"http://b.test:{port}/stay")
    await until(lambda: _at(helper, ref, "b.test"), "the person, off the list")
    await chrome.call(
        "Runtime.evaluate", {"expression": "0", "userGesture": True}, session=chrome.fence_session(page)
    )
    async with helper.client() as c:
        act = {"exec_id": "act", "command": "true", "timeout": 10}
        assert (await c.post(f"/frames/{ref}/exec", json=act)).status_code == 200

    def left() -> bool:
        return not any("b.test" in url for url in chrome.tops_of(ref).values())

    await until(left, "the page, off that site or closed")


@darwin_only
@needs_chrome
async def test_a_tab_with_no_opener_on_a_listed_page_is_not_handed(helper: Helper) -> None:
    """Conjured by a page's synthetic Cmd-click before the hand-off: the person's page is, it is not."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test", "b.test")
    await _on_listed_page(helper, ref)
    chrome = helper.chrome
    page = chrome.pages_of_task(ref)[0]
    click = (
        "document.querySelector('a').dispatchEvent("
        "new MouseEvent('click', {metaKey: true, bubbles: true, cancelable: true})); 0"
    )
    await chrome.call(
        "Runtime.evaluate", {"expression": click, "userGesture": True}, session=chrome.fence_session(page)
    )
    await until(lambda: _at(helper, ref, "/clicked-first"), "the tab, on a listed page")
    tab = next(p for p, url in chrome.tops_of(ref).items() if "/clicked-first" in url)

    await hand_over(helper, ref)
    assert chrome.handed(ref) == {page}
    await chrome.call(
        "Runtime.evaluate",
        {"expression": f"location = 'http://c.test:{port}/?data=leak'"},
        session=chrome.fence_session(tab),
    )
    await until(lambda: any(d["host"] == "c.test" for d in helper.chrome_denials(ref)), "refused")
    assert not any(host == "c.test" for host, _ in helper.site.hits)


@darwin_only
@needs_chrome
async def test_a_page_with_nowhere_listed_goes_to_about_blank(helper: Helper) -> None:
    """A sign-in's popup, still on its own host when the pass ends."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    await _on_listed_page(helper, ref)
    await hand_over(helper, ref)
    chrome = helper.chrome
    opener = chrome.pages_of_task(ref)[0]
    await chrome.call(
        "Runtime.evaluate",
        {"expression": f"window.open('http://b.test:{port}/popup'); 0", "userGesture": True},
        session=chrome.fence_session(opener),
    )
    await until(lambda: _at(helper, ref, "b.test"), "the popup, off the list")
    popup = next(page for page, url in chrome.tops_of(ref).items() if "b.test" in url)
    async with helper.client() as c:
        act = {"exec_id": "act", "command": "true", "timeout": 10}
        assert (await c.post(f"/frames/{ref}/exec", json=act)).status_code == 200
    await until(lambda: chrome.top_of(popup) == "about:blank", "the popup, emptied")


@darwin_only
@needs_chrome
async def test_a_real_sign_in_that_comes_home_keeps_its_page(helper: Helper) -> None:
    """Off the list and back by redirects, as Sign in with Amazon does: the agent finds the page the
    sign-in finished on, and nothing goes back."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    helper.site.hops["/hop-signin"] = f"http://b.test:{port}/hop-idp"
    helper.site.hops["/hop-idp"] = f"http://a.test:{port}/link-home?code=7"
    await _on_listed_page(helper, ref)
    await hand_over(helper, ref)
    await person_goes(helper, ref, f"http://a.test:{port}/hop-signin")
    await until(lambda: _at(helper, ref, "/link-home"), "home")
    assert helper.site.saw("b.test", "/hop-idp")

    cdp, _, read = await _first_read(helper, ref)
    try:
        assert read == f"http://a.test:{port}/link-home?code=7"
        assert helper.site.hits.count(("a.test", "/link-first")) == 1, "nothing went back"
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_the_ceiling_with_the_person_still_out_there(
    helper: Helper, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the ceiling the pass is over and the page back, and the person can go on where the list
    lets them: the gate is theirs to end."""
    monkeypatch.setattr(chrome_pages, "_PASS_TICK_S", 0.05)
    now = [1_000_000.0]
    monkeypatch.setattr(helper.chrome, "clock", lambda: now[0])
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    await _on_listed_page(helper, ref)
    await hand_over(helper, ref)
    await person_goes(helper, ref, f"http://b.test:{port}/slow-sign-in")
    await until(lambda: _at(helper, ref, "b.test"), "the person, off the list")

    now[0] += PASS_S  # a Mac's sleep moves this clock and no timer
    await until(lambda: _at(helper, ref, "a.test:"), "the page, back")
    assert not helper.chrome.handed(ref)
    await person_goes(helper, ref, f"http://a.test:{port}/still-mine")
    await until(lambda: helper.site.saw("a.test", "/still-mine"), "the listed site, as ever")
    await until(lambda: _at(helper, ref, "/still-mine"), "its commit")
    await person_goes(helper, ref, f"http://b.test:{port}/refused")
    await until(lambda: any(d["host"] == "b.test" for d in helper.chrome_denials(ref)), "the list holds")
    assert not helper.site.saw("b.test", "/refused")


@darwin_only
@needs_chrome
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [("PUT", "egress", {"domains": []}), ("POST", "stop-services", None)],
)
async def test_what_says_nobody_is_parked_ends_it(
    helper: Helper, method: str, path: str, body: dict[str, list[str]] | None
) -> None:
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    await _on_listed_page(helper, ref)
    await hand_over(helper, ref)
    assert helper.chrome.handed(ref)
    async with helper.client() as c:
        said = await c.request(method, f"/frames/{ref}/{path}", json=body)
    assert said.status_code < 300, said.text
    assert not helper.chrome.handed(ref)
    assert _at(helper, ref, f"a.test:{port}/link-first"), "the list the pass ran under keeps its page"
    await person_goes(helper, ref, f"http://b.test:{port}/after")
    await until(lambda: any(d["host"] == "b.test" for d in helper.chrome_denials(ref)), "the list")
    assert not helper.site.saw("b.test", "/after")


@darwin_only
@needs_chrome
async def test_the_frame_going_ends_it(helper: Helper) -> None:
    ref = await helper.frame("ta", "a.test")
    await _on_listed_page(helper, ref)
    await hand_over(helper, ref)
    async with helper.client() as c:
        assert (await c.delete(f"/frames/{ref}")).status_code == 204
    assert not helper.chrome.handed(ref)
