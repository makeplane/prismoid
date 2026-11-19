# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A hand-off's pass ends while a page is coming home, or will not go back (ADR-078 decision 21), on
the real Chrome: what the agent's first read finds. Sites and the helper fixture are
`tests/support/rigs/chrome_rig.py`; the rules, on a scripted Chrome, `tests/unit/device/test_cdp_relay_home.py`."""

from __future__ import annotations

import asyncio

import pytest

from prismoid.ai.frames.cdp_ws import WsCdp
from prismoid.device import chrome_pages
from tests.support.markers import darwin_only, needs_chrome
from tests.support.rigs.chrome_rig import (
    Helper,
    agent_acts,
    at,
    chrome_eval,
    chrome_page,
    hand_over,
    person_goes,
    until,
)
from tests.support.rigs.chrome_rig import helper as helper


async def _off_the_list(helper: Helper, ref: str) -> None:
    """The agent opened a listed page and parked; the person is on a site no list names."""
    cdp = await helper.dial(ref)
    _, session = await chrome_page(cdp)
    await at(cdp, session, helper, "a.test", "/link-first")
    await cdp.close()
    await hand_over(helper, ref)
    await person_goes(helper, ref, f"http://b.test:{helper.site.port}/signed-in")
    await until(lambda: "b.test" in str(helper.chrome.tops_of(ref)), "the person, off the list")


async def _first_read(helper: Helper, ref: str) -> tuple[WsCdp, str]:
    cdp = await helper.dial(ref)
    _, session = await chrome_page(cdp)
    return cdp, await chrome_eval(cdp, session, "location.href")


async def _on_its_way(helper: Helper, ref: str, path: str) -> None:
    """The page heads for a listed URL that has not answered, and the agent acts."""
    await person_goes(helper, ref, f"http://a.test:{helper.site.port}{path}")
    await until(lambda: helper.site.saw("a.test", path), "the request")
    await agent_acts(helper, ref)


@darwin_only
@needs_chrome
async def test_a_slow_callback_lands_before_the_read_and_stays(helper: Helper) -> None:
    ref = await helper.frame("ta", "a.test")
    helper.site.slow["/link-home"] = 1.0
    await _off_the_list(helper, ref)
    await _on_its_way(helper, ref, "/link-home")
    cdp, read = await _first_read(helper, ref)
    await cdp.close()
    assert read == f"http://a.test:{helper.site.port}/link-home"
    assert helper.site.hits.count(("a.test", "/link-first")) == 1, "nothing went back"


@darwin_only
@needs_chrome
async def test_a_callback_that_answers_204_leaves_the_signed_in_page_and_it_goes_back(helper: Helper) -> None:
    """A navigation that commits nothing: the page is where it was, off the list."""
    ref = await helper.frame("ta", "a.test")
    helper.site.no_content.add("/home-204")
    helper.site.slow["/home-204"] = 0.8
    await _off_the_list(helper, ref)
    await _on_its_way(helper, ref, "/home-204")
    cdp, read = await _first_read(helper, ref)
    await cdp.close()
    assert read == f"http://a.test:{helper.site.port}/link-first"


@darwin_only
@needs_chrome
async def test_a_callback_past_the_bound_is_given_up_on(
    helper: Helper, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chrome_pages, "_HOME_S", 0.4)
    ref = await helper.frame("ta", "a.test")
    helper.site.slow["/link-hangs"] = 3.0
    await _off_the_list(helper, ref)
    await _on_its_way(helper, ref, "/link-hangs")
    cdp, read = await _first_read(helper, ref)
    try:
        assert read == f"http://a.test:{helper.site.port}/link-first"
        await asyncio.sleep(3.2)
        assert "/link-hangs" not in await chrome_eval(cdp, (await chrome_page(cdp))[1], "location.href")
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_a_last_listed_page_that_now_answers_204_gives_way_to_about_blank(helper: Helper) -> None:
    """`Page.navigate` answers, with an `errorText`: the page has not left, whatever it says."""
    ref = await helper.frame("ta", "a.test")
    helper.site.slow["/link-first"] = 0.0  # never cached: the way back asks the server
    await _off_the_list(helper, ref)
    helper.site.no_content.add("/link-first")
    await agent_acts(helper, ref)
    cdp, read = await _first_read(helper, ref)
    await cdp.close()
    assert read == "about:blank"
