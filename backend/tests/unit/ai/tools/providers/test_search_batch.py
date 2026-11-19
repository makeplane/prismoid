# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`web_search` runs a batch of queries in ONE tool call (ADR-047): each query still bills once and
journals its engine row, the query text reaches no row, a page several queries found is listed once,
and an engine's publish date rides the row. No network: httpx.MockTransport plays the engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from prismoid.ai.tools.providers.search import SearchCaps, SearchEngine, search_toolset

SECRET = "SECRETQUERY"


@dataclass
class Journal:
    rows: list[tuple[str, bool, str, str]] = field(default_factory=list)

    async def __call__(
        self,
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        ref: str | None = None,
    ) -> None:
        self.rows.append((tool, ok, summary, denied_reason))


@dataclass
class Billed:
    count: int = 0

    async def __call__(self, n: int) -> None:
        self.count += n


def _brave(request: httpx.Request) -> httpx.Response:
    """Every query finds the shared page; each also finds a page of its own."""
    q = request.url.params["q"]
    own = q.split()[-1]
    return httpx.Response(
        200,
        json={
            "web": {
                "results": [
                    {"title": "Shared", "url": "https://shared.example/page/", "description": "both"},
                    {
                        "title": f"Own {own}",
                        "url": f"https://{own}.example/",
                        "description": "one",
                        "page_age": "2026-03-01T10:00:00",
                    },
                ]
            }
        },
    )


def _toolset(journal: Journal, billed: Billed, *, budget: int = 10) -> Any:
    toolset = search_toolset(
        ("web.search",),
        journal,
        (SearchEngine(name="brave", api_key="sk-KEY", base_url="", emit_search=billed),),
        SearchCaps(timeout_s=5.0, results=5, max_per_step=budget),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(_brave)),
    )
    assert toolset is not None
    return toolset


async def test_a_batch_of_three_queries_is_one_tool_call() -> None:
    journal, billed = Journal(), Billed()
    returned: list[str] = []

    def model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if returns:
            returned.extend(str(p.content) for p in returns)
            return ModelResponse(parts=[TextPart("done")])
        queries = [f"{SECRET} alpha", f"{SECRET} beta", f"{SECRET} gamma"]
        return ModelResponse(parts=[ToolCallPart("web_search", {"queries": queries})])

    agent: Agent[None, str] = Agent(FunctionModel(model), toolsets=[_toolset(journal, billed)])
    result = await agent.run("research")

    calls = [p for m in result.all_messages() for p in m.parts if isinstance(p, ToolCallPart)]
    assert len(calls) == 1, "three queries, one tool call"
    assert billed.count == 3, "each query is still one metered search"
    assert [row[:3] for row in journal.rows] == [("web.search", True, "brave")] * 3
    assert all(SECRET not in str(row) for row in journal.rows), "the query never reaches a row"

    [text] = returned
    assert text.count("https://shared.example/page/") == 1, "a page several queries found is listed once"
    assert "found by queries 1, 2, 3" in text
    for own in ("alpha", "beta", "gamma"):
        assert f"https://{own}.example/" in text
    assert "2026-03-01 · one" in text, "the engine's publish date rides the row"


async def test_a_batch_spends_one_search_per_query_and_says_what_it_did_not_run() -> None:
    billed = Billed()
    search = _toolset(Journal(), billed, budget=2).tools["web_search"].function

    text = await search(None, ["one a", "One  A", "two b", "three c"])

    assert billed.count == 2, "a repeat is asked once, and the budget caps the batch"
    assert "1 more queries were not run" in text
    assert "search budget (2) is spent" in await search(None, ["four d"])
