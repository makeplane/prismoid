# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Chained browser calls: a response may carry several calls, run in order, and the page comes back
once. When a call in the middle of the response changes the page, its references are gone: that call
brings the new page back, and the browser calls after it in the same response are skipped - never
sent to the sidecar, never inspected, never asked about."""

from __future__ import annotations

from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo

from prismoid.ai.capabilities.taint import tainted
from prismoid.ai.rules.approvals import Approvals
from prismoid.ai.tools.browser import BROWSER_TOOLS, FENCE_CLOSE, browser_toolset
from tests.support.fakes.browser_fakes import SNAP, FakeSidecar
from tests.support.fakes.browser_fakes import agent as _agent
from tests.support.fakes.browser_fakes import journal as _journal
from tests.support.fakes.browser_fakes import ports as _ports
from tests.support.fakes.browser_fakes import returns as _returns


class NavigatingSidecar(FakeSidecar):
    """A click on the Search button (e1.1) loads the results page; every other verb stays put."""

    async def __call__(self, cmd: dict[str, Any]) -> dict[str, Any]:
        if cmd["verb"] == "click" and cmd.get("ref") == "e1.1":
            self.where = {"url": "https://admin.example.com/results?q=ssd", "title": "Results"}
        return await super().__call__(cmd)


def _once(*calls: ToolCallPart):
    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not _returns(messages):
            return ModelResponse(parts=list(calls))
        return ModelResponse(parts=[TextPart("done")])

    return script


async def test_the_calls_after_a_call_that_changed_the_page_are_skipped_and_it_brings_the_page() -> None:
    sidecar, rows = NavigatingSidecar(), []
    toolset = browser_toolset(
        BROWSER_TOOLS, _journal(rows), _ports(sidecar, {"admin.example.com"}, []), Approvals(tainted=tainted)
    )
    script = _once(
        ToolCallPart("browser_type", {"ref": "e1.3", "text": "1tb ssd"}),
        ToolCallPart("browser_click", {"ref": "e1.1"}),
        ToolCallPart("browser_type", {"ref": "e1.3", "text": "more"}),
        ToolCallPart("browser_click", {"ref": "e1.2"}),  # a danger word: skipped, never asked about
    )
    out = await _agent(toolset, script).run("search")
    typed, clicked, typed_again, clicked_again = _returns(out.all_messages())
    assert typed.content == "ok · admin.example.com/orders · Orders"
    assert str(clicked.content).startswith("ok · admin.example.com/results · Results\n")
    assert SNAP in str(clicked.content) and FENCE_CLOSE in str(clicked.content), "the new page came back"
    for skipped in (typed_again, clicked_again):
        assert str(skipped.content).startswith("skipped: the page changed after browser_click")
    sent = [c for c in sidecar.calls if c["verb"] != "inspect"]
    assert [c["verb"] for c in sent] == ["type", "click"], (
        "the skipped calls never ran; the page came in the click's call"
    )
    assert [c.get("ref") for c in sidecar.calls if c["verb"] == "inspect"] == ["e1.3", "e1.1"]
    assert [(r[0], r[1]) for r in rows] == [
        ("browser.type", True),
        ("browser.click", True),
        ("browser.type", False),
        ("browser.click", False),
    ], "a skipped call is journaled as not run"


async def test_an_open_in_the_middle_of_a_response_skips_the_acts_after_it() -> None:
    sidecar, rows = FakeSidecar(), []
    toolset = browser_toolset(
        BROWSER_TOOLS, _journal(rows), _ports(sidecar, {"admin.example.com"}, []), Approvals(tainted=tainted)
    )
    script = _once(
        ToolCallPart("browser_open", {"url": "https://admin.example.com/orders"}),
        ToolCallPart("browser_click", {"ref": "e1.1"}),
    )
    out = await _agent(toolset, script).run("open and click")
    opened, clicked = _returns(out.all_messages())
    assert SNAP in str(opened.content)
    assert str(clicked.content).startswith("skipped: the page changed after browser_open")
    assert [c["verb"] for c in sidecar.calls] == ["open"], "the page came back in the open's own call"


async def test_a_call_that_left_the_page_where_it_was_lets_the_chain_go_on() -> None:
    sidecar, rows = FakeSidecar(), []
    toolset = browser_toolset(
        BROWSER_TOOLS, _journal(rows), _ports(sidecar, {"admin.example.com"}, []), Approvals(tainted=tainted)
    )
    script = _once(
        ToolCallPart("browser_type", {"ref": "e1.3", "text": "ada@example.com"}),
        ToolCallPart("browser_scroll", {"direction": "down"}),
        ToolCallPart("browser_click", {"ref": "e1.1"}),
    )
    out = await _agent(toolset, script).run("fill")
    assert not any(str(r.content).startswith("skipped") for r in _returns(out.all_messages()))
    acts = [c for c in sidecar.calls if c["verb"] in ("type", "scroll", "click")]
    assert [c.get("snapshot") for c in acts] == ["moved", "moved", True], "the page once, after the last call"


async def test_a_skip_holds_for_its_own_response_only() -> None:
    sidecar, rows = NavigatingSidecar(), []
    toolset = browser_toolset(
        BROWSER_TOOLS, _journal(rows), _ports(sidecar, {"admin.example.com"}, []), Approvals(tainted=tainted)
    )

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        done = len(_returns(messages))
        if done == 0:
            return ModelResponse(
                parts=[
                    ToolCallPart("browser_click", {"ref": "e1.1"}),
                    ToolCallPart("browser_click", {"ref": "e1.3"}),
                ]
            )
        if done == 2:
            return ModelResponse(parts=[ToolCallPart("browser_click", {"ref": "e1.3"})])
        return ModelResponse(parts=[TextPart("done")])

    out = await _agent(toolset, script).run("search, then pick")
    *_, again = _returns(out.all_messages())
    assert str(again.content).startswith("ok · admin.example.com/results"), "the next response acts"
