# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a real sign-in does inside a hand-off (ADR-078 decision 21), on the real Chrome: a page that
frames a captcha or an identity host, a second call of the hand-off, and what is left in the history
afterwards. Sites and the helper fixture are `tests/support/rigs/chrome_rig.py`."""

from __future__ import annotations

from prismoid.ai.frames.image_browser.sidecar.verbs import BrowserVerbs
from tests.support.markers import darwin_only, needs_chrome
from tests.support.rigs.chrome_rig import (
    Helper,
    agent_acts,
    at,
    chrome_eval,
    chrome_page,
    hand_over,
    person_goes,
    real_click,
    until,
)
from tests.support.rigs.chrome_rig import helper as helper


async def _on_listed_page(helper: Helper, ref: str) -> None:
    cdp = await helper.dial(ref)
    _, session = await chrome_page(cdp)
    await at(cdp, session, helper, "a.test", "/link-first")
    await cdp.close()


def _captcha(helper: Helper) -> None:
    """`/sign-in` frames a host of its own, which loads an image, a call and a frame of its own."""
    port, pages = helper.site.port, helper.site.pages
    pages["/sign-in"] = f"<iframe src='http://b.test:{port}/captcha'></iframe>"
    pages["/captcha"] = (
        f"<img src='http://c.test:{port}/c.png'>"
        f"<script>fetch('http://c.test:{port}/verify', {{mode: 'no-cors'}})</script>"
        f"<iframe src='http://d.test:{port}/session'></iframe>"
    )
    pages["/session"] = "the identity frame"


@darwin_only
@needs_chrome
async def test_a_sign_in_with_a_captcha_frame_loads_all_of_it_in_a_pass(helper: Helper) -> None:
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    _captcha(helper)
    await _on_listed_page(helper, ref)
    await hand_over(helper, ref)
    await person_goes(helper, ref, f"http://e.test:{port}/sign-in")
    await until(lambda: helper.site.saw("d.test", "/session"), "the frame inside the frame", seconds=1.5)
    assert helper.site.saw("b.test", "/captcha") and helper.site.saw("c.test", "/c.png")
    await until(lambda: helper.site.saw("c.test", "/verify"), "the frame's own call", seconds=1.5)
    assert not helper.chrome_denials(ref)
    assert helper.chrome.handed(ref), "and the pass goes on"


@darwin_only
@needs_chrome
async def test_the_same_frame_outside_a_pass_is_refused(helper: Helper) -> None:
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    helper.site.pages["/link-listed"] = f"<iframe src='http://b.test:{port}/captcha'></iframe>"
    await _on_listed_page(helper, ref)
    cdp = await helper.dial(ref)
    _, session = await chrome_page(cdp)
    await at(cdp, session, helper, "a.test", "/link-listed")
    await until(lambda: any(d["host"] == "b.test" for d in helper.chrome_denials(ref)), "refused")
    await cdp.close()
    assert not helper.site.saw("b.test", "/captcha")


@darwin_only
@needs_chrome
async def test_a_frame_off_the_list_does_not_outlive_the_pass(helper: Helper) -> None:
    """A listed page stays where it is, with what the person typed, and what a sign-in framed there
    goes: a session frame showing a name, or a captcha, is on a host the agent may not read."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    pages = helper.site.pages
    pages["/identity"] = f"<iframe src='http://c.test:{port}/deeper'></iframe>signed in as someone"
    pages["/deeper"] = "and a frame of its own"
    pages["/framing"] = (
        f"<input id=kept value=typed><iframe src='http://b.test:{port}/identity'></iframe>"
        f"<iframe src='http://a.test:{port}/link-first'></iframe>"
    )
    await _on_listed_page(helper, ref)
    await hand_over(helper, ref)
    await person_goes(helper, ref, f"http://a.test:{port}/framing")
    await until(lambda: helper.site.saw("c.test", "/deeper"), "the frame inside the frame")
    page = helper.chrome.pages_of_task(ref)[0]
    await until(lambda: len(helper.chrome.documents_of(page)) >= 4, "every document committed")
    await agent_acts(helper, ref)
    cdp = await helper.dial(ref)
    _, session = await chrome_page(cdp)
    # The page's own tree stops at a frame in another process, and a screenshot does not.
    held = str([document.url for document in helper.chrome.documents_of(page).values()])
    assert "b.test" not in held and "c.test" not in held, "what the sign-in framed is gone"
    assert f"a.test:{port}/link-first" in held, "a frame on the list stays"
    targets = await helper.chrome.call("Target.getTargets", {})
    assert not [t for t in targets["targetInfos"] if "b.test" in t["url"] or "c.test" in t["url"]]
    assert await chrome_eval(cdp, session, "document.getElementById('kept').value") == "typed"
    await cdp.close()


@darwin_only
@needs_chrome
async def test_a_frame_off_the_list_in_the_pages_own_process_goes_too(helper: Helper) -> None:
    """Same site, so no target of its own: only the page's session can reach it."""
    port = helper.site.port
    ref = await helper.frame("ta", "www.a.test")
    helper.site.pages["/identity"] = "signed in as someone"
    helper.site.pages["/framing"] = f"<iframe src='http://id.a.test:{port}/identity'></iframe>"
    cdp = await helper.dial(ref)
    _, session = await chrome_page(cdp)
    await at(cdp, session, helper, "www.a.test", "/link-first")
    await cdp.close()
    await hand_over(helper, ref)
    await person_goes(helper, ref, f"http://www.a.test:{port}/framing")
    page = helper.chrome.pages_of_task(ref)[0]
    await until(lambda: len(helper.chrome.documents_of(page)) == 2, "the frame committed")
    assert "id.a.test" in str(helper.chrome.documents_of(page).values())
    targets = await helper.chrome.call("Target.getTargets", {})
    assert not [t for t in targets["targetInfos"] if t["type"] == "iframe"], "the page's own process"
    await agent_acts(helper, ref)
    cdp = await helper.dial(ref)
    await chrome_page(cdp)
    await cdp.close()
    assert "id.a.test" not in str(helper.chrome.documents_of(page).values())
    assert helper.chrome.pages_of_task(ref) == [page], "and the page stays"


@darwin_only
@needs_chrome
async def test_a_target_blank_sign_in_link_is_carried(helper: Helper) -> None:
    """Chrome gives such a tab no window reference, but it names the opener all the same."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    helper.site.pages["/link-blank"] = (
        "<a target=_blank style='position:fixed;left:0;top:0;width:200px;height:200px;display:block' "
        f"href='http://b.test:{port}/sso'>go</a>"
    )
    cdp = await helper.dial(ref)
    _, session = await chrome_page(cdp)
    await at(cdp, session, helper, "a.test", "/link-blank")
    await cdp.close()
    await hand_over(helper, ref)
    await real_click(helper, ref, "/link-blank")
    await until(lambda: helper.site.saw("b.test", "/sso"), "the sign-in's tab")
    assert len(helper.chrome.handed(ref)) == 2


@darwin_only
@needs_chrome
async def test_a_second_hand_off_keeps_the_page_already_off_the_list(helper: Helper) -> None:
    """Two calls in one turn, or a retried request, while the person is on the sign-in."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    helper.site.pages["/next"] = "the second step of the sign-in"
    await _on_listed_page(helper, ref)
    page = helper.chrome.pages_of_task(ref)[0]
    await hand_over(helper, ref)
    await person_goes(helper, ref, f"http://b.test:{port}/signed-in")
    await until(lambda: "b.test" in str(helper.chrome.tops_of(ref)), "off the list")

    await hand_over(helper, ref)
    assert helper.chrome.handed(ref) == {page}
    await person_goes(helper, ref, f"http://b.test:{port}/next")
    await until(lambda: helper.site.saw("b.test", "/next"), "the next page of the sign-in")
    await until(lambda: "/next" in str(helper.chrome.tops_of(ref)), "its commit")

    await agent_acts(helper, ref)
    cdp = await helper.dial(ref)
    _, session = await chrome_page(cdp)
    read = await chrome_eval(cdp, session, "location.href")
    await cdp.close()
    assert read == f"http://a.test:{port}/link-first", "and it goes back like any other"


@darwin_only
@needs_chrome
async def test_going_back_to_a_refused_page_tells_the_model_its_host_and_path_alone(helper: Helper) -> None:
    """The history keeps a sign-in's URL whole, a code in its query and a token in its fragment, and
    what a verb says of the page is what the model reads."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    await _on_listed_page(helper, ref)
    await hand_over(helper, ref)
    await person_goes(helper, ref, f"http://b.test:{port}/signed-in?code=SECRET#token=SECRET")
    await until(lambda: "b.test" in str(helper.chrome.tops_of(ref)), "off the list")

    cdp = await helper.dial(ref)
    verbs = BrowserVerbs(cdp)
    await verbs.attach()
    said = await verbs.handle({"verb": "back", "snapshot": True})
    await cdp.close()
    assert said["url"] == f"http://b.test:{port}/signed-in"
    assert "SECRET" not in str(said)
    assert any(d["host"] == "b.test" for d in helper.chrome_denials(ref))
