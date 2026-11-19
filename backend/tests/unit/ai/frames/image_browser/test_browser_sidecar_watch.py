# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What an acting verb did, and when the page is done with it (`sidecar/watch.py`). Every acting
verb reports `change`: `navigated` (another document), `changed` (the DOM moved, or the page
scrolled) or `none`, read by a probe in an isolated world before and after. The look after an act
marks a control the last look of the same document did not have with `*`. The settle waits for the
page's own quiet - no mutation for 80 ms and no request younger than 1.5 s in flight, up to 0.8 s -
and still waits out a navigation that started, as before. A page saved to a file is read whole,
with no references, and the model's references stay live."""

from __future__ import annotations

import asyncio
import re
import sys
from typing import Any

import pytest

from tests.support.fakes.cdp_scripts import FakeCdp
from tests.support.paths import BACKEND

SIDECAR = BACKEND / "prismoid" / "ai" / "frames" / "image_browser"
sys.path.insert(0, str(SIDECAR))

from sidecar import watch  # noqa: E402
from sidecar.verbs import BrowserVerbs  # noqa: E402
from sidecar.watch import FRESH_S, Requests  # noqa: E402


def ax(*extra: dict[str, Any]) -> list[dict[str, Any]]:
    kids = ["save", "note", *(n["nodeId"] for n in extra)]
    return [
        {"nodeId": "1", "role": {"value": "RootWebArea"}, "name": {"value": "Orders"}, "childIds": kids},
        {"nodeId": "save", "role": {"value": "button"}, "name": {"value": "Save"}, "backendDOMNodeId": 12},
        {"nodeId": "note", "role": {"value": "StaticText"}, "name": {"value": "All saved"}},
        *extra,
    ]


SUGGESTION = {
    "nodeId": "opt",
    "role": {"value": "option"},
    "name": {"value": "Goa, India"},
    "backendDOMNodeId": 77,
}


class Page:
    """The page the probe reads: its document, how many mutations it has seen, where it is
    scrolled. A test moves it as the act would."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {"doc": "d1", "n": 0, "url": "https://shop.test/", "x": 0, "y": 0}

    def answer(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("executionContextId") != 7:
            return {}  # another call's function (a value set, an inspect)
        return {"result": {"value": dict(self.state, loading=False)}}


@pytest.fixture
def page() -> Page:
    return Page()


@pytest.fixture
def cdp(page: Page) -> FakeCdp:
    cdp = FakeCdp()
    cdp.answer("Page.createIsolatedWorld", {"executionContextId": 7})
    cdp.answer("Runtime.callFunctionOn", page.answer)
    cdp.answer("Accessibility.getFullAXTree", {"nodes": ax()})
    return cdp


async def attached(cdp: FakeCdp) -> BrowserVerbs:
    """Verbs on the page tab `S1`, its main frame `F`, set up as a browser's first page is."""
    cdp.answer(
        "Target.getTargets", {"targetInfos": [{"targetId": "T1", "type": "page", "url": "about:blank"}]}
    )
    cdp.answer("Page.getFrameTree", {"frameTree": {"frame": {"id": "F"}}})
    verbs = BrowserVerbs(cdp)
    await verbs.attach()
    return verbs


@pytest.fixture
async def verbs(cdp: FakeCdp) -> BrowserVerbs:
    return await attached(cdp)


async def ref(verbs: BrowserVerbs, name: str) -> str:
    snap = await verbs.handle({"verb": "snapshot"})
    line = next(line for line in str(snap["text"]).splitlines() if name in line)
    return line.lstrip("*").split("]")[0][1:]


async def test_an_act_that_moved_nothing_says_so(verbs: BrowserVerbs) -> None:
    clicked = await verbs.handle({"verb": "click", "ref": await ref(verbs, "Save")})
    assert clicked["ok"] and clicked["change"] == "none"


async def test_an_act_that_changed_the_page_says_so(verbs: BrowserVerbs, cdp: FakeCdp, page: Page) -> None:
    cdp.hooks["Input.dispatchMouseEvent"] = lambda _s, p: page.state.update(n=page.state["n"] + 1)
    clicked = await verbs.handle({"verb": "click", "ref": await ref(verbs, "Save")})
    assert clicked["change"] == "changed"


async def test_an_act_that_loaded_another_document_says_navigated(
    verbs: BrowserVerbs, cdp: FakeCdp, page: Page
) -> None:
    cdp.hooks["Input.dispatchKeyEvent"] = lambda _s, p: page.state.update(doc="d2", n=0)
    pressed = await verbs.handle({"verb": "press", "key": "Enter"})
    assert pressed["change"] == "navigated"


async def test_a_scroll_that_moved_the_page_changed_it_and_one_at_the_bottom_did_not(
    verbs: BrowserVerbs, cdp: FakeCdp, page: Page
) -> None:
    cdp.hooks["Input.dispatchMouseEvent"] = lambda _s, p: page.state.update(y=page.state["y"] + 600)
    assert (await verbs.handle({"verb": "scroll", "direction": "down"}))["change"] == "changed"
    cdp.hooks.clear()
    assert (await verbs.handle({"verb": "scroll", "direction": "down"}))["change"] == "none"


async def test_a_page_the_probe_cannot_read_reports_no_change_at_all() -> None:
    verbs = await attached(FakeCdp())  # no isolated world: an old browser, a page that refused it
    pressed = await verbs.handle({"verb": "press", "key": "Escape"})
    assert pressed["ok"] and "change" not in pressed, "unknown is never reported as none"


async def test_what_an_act_brought_onto_the_page_is_starred_in_the_look_after_it(
    verbs: BrowserVerbs, cdp: FakeCdp
) -> None:
    target = await ref(verbs, "Save")
    cdp.answer("Accessibility.getFullAXTree", {"nodes": ax(SUGGESTION)})
    typed = await verbs.handle({"verb": "click", "ref": target, "snapshot": True})
    assert '*[e2.2] option "Goa, India"' in typed["text"]
    assert typed["text"].count("*") == 1, "the button was there before: never marked"
    again = await verbs.handle({"verb": "snapshot"})
    assert "*" not in again["text"], "seen once, it is not new any more"


async def test_a_new_document_starts_with_no_marks(verbs: BrowserVerbs, cdp: FakeCdp) -> None:
    await verbs.handle({"verb": "snapshot"})
    cdp.emit("Page.frameNavigated", {"frame": {"id": "F", "url": "https://shop.test/2"}}, session="S1")
    cdp.answer("Accessibility.getFullAXTree", {"nodes": ax(SUGGESTION)})
    assert "*" not in (await verbs.handle({"verb": "snapshot"}))["text"]


async def test_the_whole_page_read_mints_no_references_and_keeps_the_models_live(verbs: BrowserVerbs) -> None:
    target = await ref(verbs, "Save")
    whole = await verbs.handle({"verb": "snapshot", "whole": True})
    assert whole["ok"] and "[e" not in whole["text"] and 'button "Save"' in whole["text"]
    assert (await verbs.handle({"verb": "click", "ref": target}))["ok"], "the reference still acts"


async def test_the_proof_checks_read_is_the_tree_as_written(verbs: BrowserVerbs) -> None:
    assert 'StaticText "All saved"' in (await verbs.handle({"verb": "snapshot", "cap": 100_000}))["text"]
    assert "StaticText" not in (await verbs.handle({"verb": "snapshot"}))["text"], "the model's look is lean"


# ── settle ──────────────────────────────────────────────────────────────────────────────────


def test_only_a_young_request_holds_the_page() -> None:
    requests = Requests()
    requests.sent({"requestId": "poll"}, now=10.0)
    assert requests.fresh(now=10.0 + FRESH_S - 0.01)
    assert not requests.fresh(now=10.0 + FRESH_S), "a long poll never ends: it never holds the page"
    requests.sent({"requestId": "xhr"}, now=20.0)
    requests.ended({"requestId": "xhr"})
    assert not requests.fresh(now=20.0)


async def drained(task: asyncio.Task[Any]) -> bool:
    for _ in range(20):
        await asyncio.sleep(0)
    return task.done()


async def test_an_act_waits_for_its_request_to_end(verbs: BrowserVerbs, cdp: FakeCdp) -> None:
    target = await ref(verbs, "Save")
    cdp.hooks["Input.dispatchMouseEvent"] = lambda _s, p: (
        p["type"] == "mouseReleased"
        and cdp.emit("Network.requestWillBeSent", {"requestId": "r1"}, session="S1")
    )
    act = asyncio.create_task(verbs.handle({"verb": "click", "ref": target}))
    assert not await drained(act), "the filter's request is still in flight"
    cdp.emit("Network.loadingFinished", {"requestId": "r1"}, session="S1")
    assert (await act)["ok"]


async def test_a_request_that_never_ends_holds_an_act_only_up_to_the_cap(
    verbs: BrowserVerbs, cdp: FakeCdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch, "SETTLE_S", 0.05)
    target = await ref(verbs, "Save")
    cdp.emit("Network.requestWillBeSent", {"requestId": "stream"}, session="S1")
    assert (await asyncio.wait_for(verbs.handle({"verb": "click", "ref": target}), timeout=5))["ok"]


async def test_a_tab_in_the_background_never_holds_the_page(verbs: BrowserVerbs, cdp: FakeCdp) -> None:
    cdp.emit("Network.requestWillBeSent", {"requestId": "ad"}, session="S2")
    act = asyncio.create_task(verbs.handle({"verb": "press", "key": "Escape"}))
    assert await drained(act)


async def test_a_navigation_that_started_is_waited_out_to_its_load(verbs: BrowserVerbs, cdp: FakeCdp) -> None:
    cdp.hooks["Input.dispatchKeyEvent"] = lambda _s, p: (
        p["type"] == "keyUp" and cdp.emit("Page.frameStartedLoading", {"frameId": "F"}, session="S1")
    )
    act = asyncio.create_task(verbs.handle({"verb": "press", "key": "Enter"}))
    assert not await drained(act), "the page is loading"
    cdp.emit("Page.loadEventFired", {}, session="S1")
    assert (await act)["ok"]


# ── what the probe cannot see, and the settle's cap ────────────────

FIELD = {"nodeId": "q", "role": {"value": "textbox"}, "name": {"value": "Search"}, "backendDOMNodeId": 31}


@pytest.mark.parametrize("verb", ["type", "fill", "select"])
async def test_a_value_set_on_a_field_is_never_reported_as_no_change(
    verb: str, verbs: BrowserVerbs, cdp: FakeCdp
) -> None:
    """A value is a property, not a mutation: the observer never sees typing land, so a field verb
    that worked says nothing about `change` rather than a `none` that invites a retype."""
    cdp.answer("Accessibility.getFullAXTree", {"nodes": ax(FIELD)})
    cdp.answer("DOM.resolveNode", {"object": {"objectId": "o"}})
    target = await ref(verbs, "Search")
    cmd = {
        "type": {"text": "ssd"},
        "fill": {"fields": [{"ref": target, "value": "ssd"}]},
        "select": {"value": "a"},
    }
    done = await verbs.handle({"verb": verb, "ref": target, **cmd[verb]})
    assert done["ok"] and "change" not in done


@pytest.mark.parametrize("role", ["checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio"])
async def test_a_click_on_a_checkbox_or_radio_is_never_reported_as_no_change(
    role: str, verbs: BrowserVerbs, cdp: FakeCdp
) -> None:
    """`checked` is a property, as a field's value is: a click that ticked a box mutated nothing
    the observer sees, so it says nothing about `change` rather than a `none` that invites a reclick."""
    box = {"nodeId": "box", "role": {"value": role}, "name": {"value": "Gift wrap"}, "backendDOMNodeId": 31}
    cdp.answer("Accessibility.getFullAXTree", {"nodes": ax(box)})
    snap = await verbs.handle({"verb": "snapshot"})
    target = re.search(rf'\[(\S+)\] {role} "Gift wrap"', str(snap["text"]))
    assert target is not None
    clicked = await verbs.handle({"verb": "click", "ref": target[1]})
    assert clicked["ok"] and "change" not in clicked


async def test_a_value_verb_that_changed_the_page_still_says_so(
    verbs: BrowserVerbs, cdp: FakeCdp, page: Page
) -> None:
    cdp.answer("Accessibility.getFullAXTree", {"nodes": ax(FIELD)})
    cdp.answer("DOM.resolveNode", {"object": {"objectId": "o"}})
    cdp.hooks["Input.insertText"] = lambda _s, p: page.state.update(n=page.state["n"] + 3)
    typed = await verbs.handle({"verb": "type", "ref": await ref(verbs, "Search"), "text": "goa"})
    assert typed["change"] == "changed"


def test_a_jump_within_the_page_is_not_a_navigation() -> None:
    """The chain (`browser_policy.page_changed`) keeps the page on a `#fragment` move: so does the probe."""
    at = {"doc": "d1", "n": 0, "url": "https://shop.test/a", "x": 0, "y": 0}
    assert watch.change(at, {**at, "url": "https://shop.test/a#reviews", "y": 900}) == "changed"
    assert watch.change(at, {**at, "url": "https://shop.test/b"}) == "navigated"


async def test_a_page_stuck_loading_holds_an_act_only_up_to_the_overall_cap(
    verbs: BrowserVerbs, cdp: FakeCdp, page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A document that never finishes parsing (or a refresh loop) re-armed the load wait for
    ever, holding the sidecar's lock: one cap from the act's start, and the act says so."""
    monkeypatch.setattr(watch, "LOAD_S", 0.05)
    monkeypatch.setattr(watch, "SETTLE_S", 0.02)
    cdp.answer("Runtime.callFunctionOn", lambda p: {"result": {"value": dict(page.state, loading=True)}})
    pressed = await asyncio.wait_for(verbs.handle({"verb": "press", "key": "Enter"}), timeout=3)
    assert pressed["ok"] and pressed["settled"] is False


async def test_an_act_on_a_page_that_settled_says_nothing_of_settling(verbs: BrowserVerbs) -> None:
    assert "settled" not in await verbs.handle({"verb": "press", "key": "Escape"})


@pytest.mark.parametrize("verb", ["type", "select", "scroll"])
async def test_a_navigation_any_act_starts_is_waited_out_even_after_an_earlier_load(
    verb: str, verbs: BrowserVerbs, cdp: FakeCdp
) -> None:
    """An earlier page's load is not this act's: a select whose change reloads the list, a field
    that navigates as it is typed in, a scroll that loads the next page each wait their own load."""
    cdp.answer("Accessibility.getFullAXTree", {"nodes": ax(FIELD)})
    cdp.answer("DOM.resolveNode", {"object": {"objectId": "o"}})
    target = await ref(verbs, "Search")
    cdp.emit("Page.loadEventFired", {}, session="S1")  # the page this act starts on, long loaded
    starts = {"type": "Input.insertText", "select": "DOM.resolveNode", "scroll": "Input.dispatchMouseEvent"}
    cdp.hooks[starts[verb]] = lambda _s, _p: cdp.emit(
        "Page.frameStartedLoading", {"frameId": "F"}, session="S1"
    )
    cmd = {"type": {"ref": target, "text": "goa"}, "select": {"ref": target, "value": "a"}, "scroll": {}}
    act = asyncio.create_task(verbs.handle({"verb": verb, **cmd[verb]}))
    assert not await drained(act), "the navigation it started is loading"
    cdp.emit("Page.loadEventFired", {}, session="S1")
    assert (await act)["ok"]
