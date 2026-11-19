# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""One sidecar call per look-and-act, waits on the page's own signals, one schema per browser tool.

An act brings its page back in its own call: always when it ends its response, and mid-response
only when it left the page (the sidecar's `moved`), so a look never expires the references a
response still holds. `wait` reads the page as it changes and never sleeps. `browser_snapshot` and
`browser_screenshot` keep one schema on every computer; one that cannot save says so."""

from __future__ import annotations

import asyncio
import sys
from typing import Any, cast

import pytest

from prismoid.ai.capabilities.taint import tainted
from prismoid.ai.rules.approvals import Approvals
from prismoid.ai.tools.browser import BROWSER_TOOLS, NO_SAVE, BrowserPorts, browser_toolset
from tests.support.paths import BACKEND

SIDECAR = BACKEND / "prismoid" / "ai" / "frames" / "image_browser"
sys.path.insert(0, str(SIDECAR))

from sidecar import watch  # noqa: E402
from sidecar.cdp import CdpError  # noqa: E402
from sidecar.scripts import APPEARS_JS  # noqa: E402
from sidecar.verbs import BrowserVerbs  # noqa: E402


class FakeCdp:
    """A scripted CDP connection: `answer` a method (a reply, or a function of its params), `hooks`
    run as a call arrives, `emit` an event; anything unscripted answers `{}`."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Any]] = {}
        self._answers: dict[str, Any] = {
            "Page.getNavigationHistory": {"currentIndex": 0, "entries": [{"url": "https://shop.test/"}]},
            "DOM.getBoxModel": {"model": {"content": [0, 0, 10, 0, 10, 10, 0, 10]}},
            "DOM.describeNode": {"node": {"attributes": []}},
            "Page.captureScreenshot": {"data": ""},
            "Target.attachToTarget": {"sessionId": "S1"},
        }
        self.hooks: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def emit(self, event: str, params: dict[str, Any], *, session: str) -> None:
        for handler in self._handlers.get(event, []):
            handler({"method": event, "params": params, "sessionId": session})

    def answer(self, method: str, result: Any) -> None:
        self._answers[method] = result

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109
    ) -> dict[str, Any]:
        if (hook := self.hooks.get(method)) is not None:
            hook(session_id, params)
        answer = self._answers.get(method, {})
        reply = answer(params or {}) if callable(answer) else answer
        assert isinstance(reply, dict)
        return cast("dict[str, Any]", reply)

    async def close(self) -> None:
        pass


class Page:
    """What the probe reads of the page (its document, mutations, address); a test moves it."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {"doc": "d1", "n": 0, "url": "https://shop.test/", "x": 0, "y": 0}

    def answer(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("executionContextId") != 7:
            return {}
        return {"result": {"value": dict(self.state, loading=False)}}


AX = [
    {"nodeId": "1", "role": {"value": "RootWebArea"}, "name": {"value": "Orders"}, "childIds": ["save"]},
    {"nodeId": "save", "role": {"value": "button"}, "name": {"value": "Save"}, "backendDOMNodeId": 12},
]


class Shown:
    """What the page shows to `APPEARS_JS`: each answer in turn (a bool, or a document torn down)."""

    def __init__(self, page: Page, *answers: bool | str) -> None:
        self.page, self.answers, self.texts = page, list(answers), list[str]()
        self.cdp: FakeCdp | None = None

    def __call__(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("functionDeclaration") != APPEARS_JS:
            return self.page.answer(params)
        self.texts.append(params["arguments"][0]["value"])
        answer = self.answers.pop(0)
        if answer == "torn down":
            assert self.cdp is not None
            self.cdp.emit("Page.loadEventFired", {}, session="S1")  # the next document loads
            raise CdpError(-32000, "Execution context was destroyed.")
        return {"result": {"value": answer}}


async def _verbs(page: Page, shown: Shown | None = None) -> tuple[BrowserVerbs, FakeCdp]:
    cdp = FakeCdp()
    cdp.answer("Page.createIsolatedWorld", {"executionContextId": 7})
    cdp.answer("Runtime.callFunctionOn", shown or page.answer)
    cdp.answer("Accessibility.getFullAXTree", {"nodes": AX})
    cdp.answer(
        "Target.getTargets", {"targetInfos": [{"targetId": "T1", "type": "page", "url": "about:blank"}]}
    )
    cdp.answer("Page.getFrameTree", {"frameTree": {"frame": {"id": "F"}}})
    if shown is not None:
        shown.cdp = cdp
    verbs = BrowserVerbs(cdp)
    await verbs.attach()
    return verbs, cdp


async def test_an_act_that_left_the_page_brings_its_look_in_the_same_call() -> None:
    page = Page()
    verbs, cdp = await _verbs(page)
    cdp.hooks["Input.dispatchKeyEvent"] = lambda _s, _p: page.state.update(
        doc="d2", url="https://shop.test/2"
    )
    pressed = await verbs.handle({"verb": "press", "key": "Enter", "snapshot": "moved"})
    assert pressed["ok"] and pressed["moved"] is True
    assert 'button "Save"' in pressed["text"], "the new page came back with the act"


async def test_an_act_that_stayed_brings_no_look_and_the_references_stay_live() -> None:
    verbs, _cdp = await _verbs(Page())
    snap = await verbs.handle({"verb": "snapshot"})
    target = next(line for line in snap["text"].splitlines() if "Save" in line).split("]")[0][1:]
    clicked = await verbs.handle({"verb": "click", "ref": target, "snapshot": "moved"})
    assert clicked["ok"] and clicked["moved"] is False and "text" not in clicked
    assert (await verbs.handle({"verb": "click", "ref": target}))["ok"], "no look minted new references"


async def test_wait_answers_when_the_text_shows_and_never_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    page = Page()
    shown = Shown(page, True)
    verbs, _cdp = await _verbs(page, shown)

    async def no_sleep(_s: float) -> None:
        raise AssertionError("a wait never sleeps")

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    assert await verbs.handle({"verb": "wait", "for": "Saved"}) == {"ok": True}
    assert shown.texts == ["Saved"]


async def test_wait_reads_the_next_document_once_it_loads() -> None:
    page = Page()
    shown = Shown(page, "torn down", True)
    verbs, _cdp = await _verbs(page, shown)
    assert await verbs.handle({"verb": "wait", "for": "Order placed"}) == {"ok": True}
    assert shown.texts == ["Order placed", "Order placed"]


async def test_wait_says_when_the_text_never_showed() -> None:
    page = Page()
    verbs, _cdp = await _verbs(page, Shown(page, False))
    waited = await verbs.handle({"verb": "wait", "for": "Saved"})
    assert waited == {"ok": False, "error": "waited 10 s and 'Saved' did not appear"}


@pytest.mark.parametrize("target", [500, "", None])
async def test_wait_takes_a_text_never_a_time(target: object) -> None:
    verbs, _cdp = await _verbs(Page())
    waited = await verbs.handle({"verb": "wait", "for": target})
    assert waited == {"ok": False, "error": "wait needs a text that should appear on the page"}


@pytest.mark.parametrize(
    ("verb", "before", "result", "moved"),
    [
        ("click", {"url": "https://a.example/x"}, {"ok": True, "url": "https://a.example/y"}, True),
        ("click", {"url": "https://a.example/x"}, {"ok": True, "url": "https://a.example/x#r"}, False),
        ("click", None, {"ok": True, "url": "https://a.example/y"}, False),  # unknown keeps the chain
        ("click", None, {"ok": True, "url": "https://a.example/y", "change": "navigated"}, True),
        (
            "click",
            {"url": "https://a.example/x"},
            {"ok": True, "url": "https://a.example/y", "change": "changed"},
            False,
        ),
        ("open", {"url": "https://a.example/x"}, {"ok": True, "change": "none"}, True),
        ("back", None, {"ok": True, "note": "already at the first page"}, False),
        ("click", {"url": "https://a.example/x"}, {"ok": True, "tab": "opened", "change": "none"}, True),
    ],
)
def test_what_left_the_page(
    verb: str, before: dict[str, Any] | None, result: dict[str, Any], moved: bool
) -> None:
    assert watch.left(verb, result, before) is moved


# ── one schema on every computer ─────────────────────────────────────────────────────────────


class Sidecar:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def browse(self, cmd: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(cmd)
        return {"ok": True, "text": "x", "png_b64": "", "url": "https://a.example/", "title": "A"}


async def _allow(_hosts: list[str]) -> bool:
    return True


async def _save(path: str, _data: bytes) -> str:
    return path


def _toolset(sidecar: Sidecar, *, saves: bool, rows: list[tuple[str, bool]] | None = None) -> Any:
    async def journal(tool: str, ok: bool, *_a: Any, **_k: Any) -> None:
        if rows is not None:
            rows.append((tool, ok))

    ports = BrowserPorts(browse=sidecar.browse, allow=_allow, save=_save if saves else None)
    toolset = browser_toolset(BROWSER_TOOLS, journal, ports, Approvals(tainted=tainted))
    assert toolset is not None
    return toolset


@pytest.mark.parametrize("name", ["browser_snapshot", "browser_screenshot", "browser_wait"])
def test_a_browser_tool_has_one_schema_on_every_computer(name: str) -> None:
    cloud, device = (_toolset(Sidecar(), saves=s).tools[name].tool_def for s in (False, True))
    assert cloud.parameters_json_schema == device.parameters_json_schema
    assert cloud.description == device.description


def test_wait_offers_a_text_only() -> None:
    schema = _toolset(Sidecar(), saves=False).tools["browser_wait"].tool_def.parameters_json_schema
    assert set(schema["properties"]) == {"for_text"} and schema["required"] == ["for_text"]


class _Ctx:
    tool_call_id = "c1"
    messages: tuple[Any, ...] = ()
    tool_call_approved = False


@pytest.mark.parametrize(
    ("name", "path"), [("browser_snapshot", "/work/p.md"), ("browser_screenshot", "/work/p.png")]
)
async def test_a_computer_that_cannot_save_says_so_and_never_calls_the_browser(name: str, path: str) -> None:
    sidecar, rows = Sidecar(), list[tuple[str, bool]]()
    answer = await _toolset(sidecar, saves=False, rows=rows).tools[name].function(_Ctx(), save_as=path)
    assert answer == NO_SAVE and sidecar.calls == []
    assert rows == [(name.replace("_", ".", 1), False)], "journaled as a call that did nothing"
