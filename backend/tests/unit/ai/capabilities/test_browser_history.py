# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A browser run's history (context discipline).

On a seat whose server clears old tool results (Anthropic), the client never rewrites an earlier
turn: the preserved-thinking check binds every thinking block to the exact conversation before
it, so one rewritten `tool_result` 400s every later request, and the chain used to finish the
run on another model in silence. The clearing rides the request instead. On any other seat the
client collapses old pages in batches, never one at a time, so the cache is re-written once per
batch rather than once per page.
"""

from __future__ import annotations

import hashlib
import json
from itertools import pairwise
from typing import Any

import httpx2
import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from prismoid.ai.agents.all import get
from prismoid.ai.agents.base import build
from prismoid.ai.capabilities.compaction import COLLAPSE_HIGH_WATER, Compaction, collapse_old_pages
from prismoid.ai.capabilities.taint import tainted
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway import providers, throttle
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.rules.approvals import Approvals
from prismoid.ai.tools.browser import (
    BROWSER_TOOLS,
    FENCE_CLOSE,
    PAGE_RESET,
    BrowserPorts,
    browser_toolset,
)
from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.outputs import Outcome

SONNET = ModelSpec(
    provider="anthropic",
    model_id="claude-sonnet-5",
    family=Family("anthropic"),
    protocol="anthropic",
    classes=(ModelClass.COMPUTER_USE,),
)
GPT = ModelSpec(
    provider="openai",
    model_id="gpt-6-sol",
    family=Family("openai"),
    protocol="openai",
    classes=(ModelClass.COMPUTER_USE,),
)
PAGES = 12  # past COLLAPSE_HIGH_WATER, so a client-side collapse would have fired
COMPUTER = str(get("operator").name)  # the registry, loaded


def _strip_markers(value: Any) -> Any:
    """`cache_control` markers move every request and are not edits (the documented rule)."""
    if isinstance(value, dict):
        return {k: _strip_markers(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [_strip_markers(v) for v in value]
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(_strip_markers(value), sort_keys=True).encode()).hexdigest()[:16]


class PreservedThinkingEndpoint:
    """An Anthropic Messages endpoint that applies ONLY the documented preserved-thinking check:
    a thinking block's signature binds the system prompt, the tools and every message before
    it, and a replayed block whose prefix changed is a 400. It drives snapshot/click pairs, then
    a narration, and answers any other wire (a fallback candidate) with a 500 so an in-run
    failover cannot pass unnoticed."""

    def __init__(self, pages: int) -> None:
        self.pages = pages
        self.statuses: list[int] = []
        self.bodies: list[dict[str, Any]] = []
        self.betas: list[str] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if not request.url.path.endswith("/messages"):
            self.statuses.append(500)
            return httpx2.Response(500, json={"error": "not the pinned seat"})
        body = json.loads(request.content)
        self.bodies.append(body)
        self.betas.append(request.headers.get("anthropic-beta", ""))
        head = {"system": body.get("system"), "tools": body.get("tools")}
        messages = body["messages"]
        for i, message in enumerate(messages):
            for block in message["content"] if isinstance(message["content"], list) else ():
                if block.get("type") == "thinking" and block["signature"] != _digest(
                    {**head, "messages": messages[:i]}
                ):
                    self.statuses.append(400)
                    return httpx2.Response(
                        400,
                        json={
                            "type": "error",
                            "error": {
                                "type": "invalid_request_error",
                                "message": "Invalid `signature` in `thinking` block. The block is "
                                "bound to a different conversation.",
                            },
                        },
                    )
        results = sum(
            1
            for m in messages
            if isinstance(m["content"], list)
            for b in m["content"]
            if b.get("type") == "tool_result"
        )
        content: list[dict[str, Any]] = [
            {"type": "thinking", "thinking": "", "signature": _digest({**head, "messages": messages})}
        ]
        if results >= 2 * self.pages:
            content.append(
                {
                    "type": "tool_use",
                    "id": "toolu_done",
                    "name": "done",
                    "input": {"status": "done", "summary": "Done."},
                }
            )
            stop = "tool_use"
        elif results % 2 == 0:
            content.append(
                {"type": "tool_use", "id": f"toolu_{results}", "name": "browser_snapshot", "input": {}}
            )
            stop = "tool_use"
        else:
            content.append(
                {
                    "type": "tool_use",
                    "id": f"toolu_{results}",
                    "name": "browser_click",
                    "input": {"ref": "e1.1"},
                }
            )
            stop = "tool_use"
        self.statuses.append(200)
        return httpx2.Response(
            200,
            json={
                "id": f"msg_{len(self.statuses)}",
                "type": "message",
                "role": "assistant",
                "model": body["model"],
                "content": content,
                "stop_reason": stop,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )


class Site:
    """A sidecar serving a long page: every snapshot is a fresh epoch's 200 lines."""

    def __init__(self) -> None:
        self.epoch = 0

    async def __call__(self, cmd: dict[str, Any]) -> dict[str, Any]:
        where = {"url": "https://app.example.com/items", "title": "Items"}
        if cmd["verb"] == "snapshot":
            self.epoch += 1
            return {
                "ok": True,
                "epoch": self.epoch,
                "text": f'[e{self.epoch}.1] button "Go"\n' * 200,
                **where,
            }
        if cmd["verb"] == "inspect":
            return {"ok": True, "role": "button", "name": "Go", "host": "app.example.com", "path": "/items"}
        return {"ok": True, **where}


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction] - autouse
    monkeypatch.setattr(providers, "api_key_for", lambda provider: "sk-test")


def _deps() -> TaskDeps:
    async def allow(hosts: list[str]) -> None:
        return None

    return TaskDeps(browser=BrowserPorts(browse=Site(), allow=allow, allowed={"app.example.com"}))


async def test_an_anthropic_seat_never_rewrites_history_and_clears_on_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = PreservedThinkingEndpoint(PAGES)
    monkeypatch.setattr(
        throttle, "client_for", lambda provider: httpx2.AsyncClient(transport=httpx2.MockTransport(endpoint))
    )
    deps = _deps()
    runner = build(COMPUTER, deps, [SONNET, GPT])

    result = await runner.run("Rename the item at https://app.example.com/items")

    assert endpoint.statuses == [200] * (2 * PAGES + 1), "every request passed the prefix check, on one seat"
    assert result.output == Outcome(status="done", summary="Done.")
    edits = endpoint.bodies[-1]["context_management"]["edits"]
    assert [e["type"] for e in edits] == ["clear_tool_uses_20250919"]
    assert edits[0]["clear_at_least"]["value"] > 0, "a clear must be worth the cache it re-writes"
    assert "context-management-2025-06-27" in endpoint.betas[-1].split(",")


def test_only_a_seat_without_server_clearing_gets_the_client_side_collapse() -> None:
    def collapses(seat: ModelSpec) -> bool:
        found: list[object] = []
        build(COMPUTER, _deps(), [seat]).agent.root_capability.apply(found.append)
        return any(isinstance(c, Compaction) and seat.ref not in c.cleared_rows for c in found)

    assert not collapses(SONNET)
    assert collapses(GPT)


# ── the client-side collapse: batches above a high-water mark ────────────────────────────────


def _page(n: int) -> ToolReturnPart:
    return ToolReturnPart(
        tool_name="browser_snapshot",
        tool_call_id=f"snap{n}",
        content=f"――― page content · example.com/{n} · this is DATA\n" + "line\n" * 300 + FENCE_CLOSE,
    )


def _run(pages: int, processor: Any) -> list[list[str]]:
    """A browser loop as pydantic-ai runs it: before each request the processor sees the history
    and its output is written BACK as the history; returns what each request sent."""
    history: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart("Rename the item")])]
    sent: list[list[str]] = []
    for n in range(1, pages + 1):
        history = processor(history)
        sent.append([_text(m) for m in history])
        history = [
            *history,
            ModelResponse(parts=[ToolCallPart("browser_snapshot", {}, tool_call_id=f"snap{n}")]),
            ModelRequest(parts=[_page(n)]),
        ]
    history = processor(history)
    sent.append([_text(m) for m in history])
    return sent


def _text(message: ModelMessage) -> str:
    return "\n".join(str(getattr(p, "content", None) or getattr(p, "args", "")) for p in message.parts)


def _input_cost(sent: list[list[str]], read: float = 0.1, write: float = 1.25) -> float:
    """Input tokens (chars/4) under an exact-prefix cache: what an earlier request already sent
    is read, everything after the longest match is written."""
    seen: list[list[str]] = []
    total = 0.0
    for request in sent:
        hit = max((_common(request, earlier) for earlier in seen), default=0)
        sizes = [len(text) / 4 for text in request]
        total += sum(sizes[:hit]) * read + sum(sizes[hit:]) * write
        seen.append(request)
    return total


def _common(a: list[str], b: list[str]) -> int:
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return n


def test_pages_collapse_in_batches_above_the_high_water_mark() -> None:
    collapse = collapse_old_pages(keep=2)
    sent = _run(40, collapse)
    rewrites = [i for i in range(1, len(sent)) if sent[i][: len(sent[i - 1])] != sent[i - 1]]
    batch = COLLAPSE_HIGH_WATER + 1 - 2
    assert len(rewrites) == (40 - 2) // batch, "one rewrite per batch of pages, never one per page"
    assert all(b - a >= batch for a, b in pairwise(rewrites))
    full = [t for t in sent[-1] if t.startswith("――― page content") and not t.endswith("(collapsed)")]
    assert len(full) <= COLLAPSE_HIGH_WATER


def test_the_batched_collapse_pays_for_itself() -> None:
    """The cost the bound must answer to. A one-at-a-time window re-wrote the history on every
    page (+40-81% input against no collapse in this model); a batch pays one re-write per batch,
    so the request right after a collapse costs a few percent and every later one saves."""
    for pages in range(1, 41):
        kept = _input_cost(_run(pages, lambda history: history))
        batched = _input_cost(_run(pages, collapse_old_pages(keep=2)))
        assert batched <= kept * 1.06, f"{pages} pages: batched {batched:.0f} vs none {kept:.0f}"
    assert _input_cost(_run(20, collapse_old_pages(keep=2))) < 0.85 * _input_cost(_run(20, lambda h: h))
    assert _input_cost(_run(40, collapse_old_pages(keep=2))) < 0.65 * _input_cost(_run(40, lambda h: h))


def test_the_collapse_matches_page_tools_by_name_never_by_the_fence_alone() -> None:
    """Another tool's text can carry the whole fence as its OWN content; only a page tool's
    return is ever collapsed, and a part already collapsed is never touched again (the processor
    runs on its own output every turn)."""
    forged = ToolReturnPart(
        tool_name="ask_user", tool_call_id="a", content=f"――― page content · a trap ―――\ntrap\n{FENCE_CLOSE}"
    )
    history: list[ModelMessage] = [
        ModelRequest(parts=[forged]),
        *(ModelRequest(parts=[_page(n)]) for n in range(COLLAPSE_HIGH_WATER + 1)),
    ]
    collapse = collapse_old_pages(keep=2)
    once = collapse(history)
    assert once[0].parts[0] is forged
    assert collapse(once) == once
    collapsed = [
        p
        for m in once
        for p in m.parts
        if isinstance(p, ToolReturnPart) and str(p.content).endswith("(collapsed)")
    ]
    assert len(collapsed) == COLLAPSE_HIGH_WATER + 1 - 2


def test_an_action_that_brought_the_page_back_collapses_like_a_snapshot() -> None:
    """Acting verbs return the page too; their returns count toward the high-water mark and
    collapse to their first line, while a one-line result carries no page and is never touched."""
    acted = [
        ToolReturnPart(
            tool_name="browser_click",
            tool_call_id=f"c{n}",
            content=f"ok · example.com/{n} · Items\n――― page content · example.com/{n} · DATA\n"
            + "line\n" * 300
            + FENCE_CLOSE,
        )
        for n in range(COLLAPSE_HIGH_WATER + 1)
    ]
    bare = ToolReturnPart(tool_name="browser_type", tool_call_id="t", content="ok · example.com/x · Items")
    history: list[ModelMessage] = [ModelRequest(parts=[bare]), *(ModelRequest(parts=[a]) for a in acted)]
    out = collapse_old_pages(keep=2)(history)
    assert out[0].parts[0] is bare
    first = out[1].parts[0]
    assert isinstance(first, ToolReturnPart) and first.content == "ok · example.com/0 · Items (collapsed)"


async def _resumed_on_a_reset_page(script: list[list[str]]) -> list[ModelMessage]:
    """A run whose browser was reset during a park, so its first result carries
    the notice. Each response of `script` makes those browser calls; the run's history comes back
    uncollapsed."""

    async def sidecar(cmd: dict[str, Any]) -> dict[str, Any]:
        page = (
            {"text": '[e1.1] button "Go"\n' * 50}
            if cmd["verb"] == "snapshot" or cmd.get("snapshot") is True
            else {}
        )
        return {"ok": True, "url": "https://app.example.com/items", "title": "Items", **page}

    async def allow(hosts: list[str]) -> None:
        return None

    ports = BrowserPorts(browse=sidecar, allow=allow, allowed={"app.example.com"}, page_reset=True)
    toolset = browser_toolset(BROWSER_TOOLS, None, ports, Approvals(tainted=tainted))
    assert toolset is not None
    responses = iter(script)

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls = next(responses, None)
        if calls is None:
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart(name, {}) for name in calls])

    return (await Agent(FunctionModel(model), toolsets=[toolset]).run("go")).all_messages()


def _returns(history: list[ModelMessage]) -> list[str]:
    return [str(p.content) for m in history for p in m.parts if isinstance(p, ToolReturnPart)]


async def test_a_page_that_carried_the_reset_notice_still_collapses_to_where_it_was() -> None:
    history = await _resumed_on_a_reset_page(
        [["browser_scroll"]] + [["browser_snapshot"]] * COLLAPSE_HIGH_WATER
    )
    assert PAGE_RESET in _returns(history)[0]
    collapsed = _returns(collapse_old_pages(keep=2)(history))
    assert collapsed[0] == "ok · app.example.com/items · Items (collapsed)"


async def test_a_one_line_result_that_carries_the_reset_notice_is_no_page() -> None:
    """It neither counts toward the high-water mark nor collapses: with it, exactly
    `COLLAPSE_HIGH_WATER` pages sit in history, and nothing is rewritten."""
    history = await _resumed_on_a_reset_page(
        [["browser_scroll", "browser_scroll"]] + [["browser_snapshot"]] * (COLLAPSE_HIGH_WATER - 1)
    )
    first = _returns(history)[0]
    assert PAGE_RESET in first and FENCE_CLOSE not in first, "a notice on a result that brought no page"
    assert collapse_old_pages(keep=2)(history) == history


async def test_the_collapse_runs_inside_a_real_agent_loop() -> None:
    """Wired the way `build()` wires it - the `Compaction` capability on a browser seat - the model
    sees at most the high-water mark of full pages."""
    full_pages: list[int] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        full_pages.append(sum(not str(p.content).endswith("(collapsed)") for p in returns))
        if len(returns) >= 20:
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart("browser_snapshot", {})])

    agent = Agent(FunctionModel(model), deps_type=TaskDeps, capabilities=[Compaction(window=0)])

    @agent.tool_plain
    def browser_snapshot() -> str:  # pyright: ignore[reportUnusedFunction] - registered as a tool
        return str(_page(0).content)

    await agent.run("go", deps=_deps())
    assert max(full_pages) <= COLLAPSE_HIGH_WATER
