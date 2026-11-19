# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Two holes in the run guard's brakes.

* A guarded agent that may not ask (the scatter map, a bot without the ask grant) cannot park on the
  budget gate: at the cap - or blind, after `SPEND_READ_FAILURES_TO_ARM` failed reads - it is held
  instead, every call but a way to finish, and ends with what it has. Before, its calls ran on past
  the cap to the request ceiling.
* The same web search, retyped with another case or spacing, is the same search: engines read it
  the same way, so it is a repeat. Every other tool's arguments stay exact."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic_ai import Agent, Tool, ToolReturn
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
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.capabilities.loops import SPEND_READ_FAILURES_TO_ARM, RunGuard
from prismoid.ai.deps import TaskDeps
from prismoid.ai.tools.call_rows import not_run
from prismoid.ai.tools.frame_tools import PUBLISH
from prismoid.schemas.guard import BUDGET_FINISH, STALL_HELD
from prismoid.schemas.plan import BudgetFigures
from tests.support.guard import watched

_AT_CAP = BudgetFigures(ceiling_usd=Decimal("1.00"), spent_usd=Decimal("1.00"))


def _scripted(calls: list[tuple[str, dict[str, Any]]]) -> FunctionModel:
    """One call per request, in order, then a text ending."""
    asked = 0

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal asked
        asked += 1
        if asked <= len(calls):
            name, args = calls[asked - 1]
            return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id=f"c{asked}")])
        return ModelResponse(parts=[TextPart("end")])

    return FunctionModel(model)


async def _unparked(
    calls: list[tuple[str, dict[str, Any]]], tools: list[Tool[TaskDeps]], **deps: Any
) -> tuple[RunGuard, str]:
    guard, runs = watched(can_park=False, outcome=False)  # the scatter map's mount: it may not ask
    agent = Agent(
        _scripted(calls),
        deps_type=TaskDeps,
        output_type=str,  # no `DeferredToolRequests`: a deferral has nowhere to go
        toolsets=[FunctionToolset(tools)],
        capabilities=[guard],
    )
    output = (await agent.run("go", deps=TaskDeps(**deps))).output
    return runs[0], output


class _Spend:
    def __init__(self, figures: BudgetFigures | None) -> None:
        self.figures = figures

    async def __call__(self) -> BudgetFigures:
        if self.figures is None:
            raise RuntimeError("db outage")
        return self.figures


async def test_at_the_cap_a_run_that_cannot_park_is_held_and_ends_with_what_it_has() -> None:
    ran: list[int] = []
    rows: list[tuple[str, str]] = []

    async def journal(
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        ref: str | None = None,
    ) -> None:
        rows.append((tool, denied_reason))

    def web_fetch(n: int) -> str:
        ran.append(n)
        return "a page"

    calls: list[tuple[str, dict[str, Any]]] = [("web_fetch", {"n": n}) for n in range(5)]
    _, output = await _unparked(calls, [Tool(web_fetch)], spend=_Spend(_AT_CAP), journal_tool=journal)
    assert output == "end"
    assert ran == [], "no call runs past the cap"
    assert rows == [("web.fetch", STALL_HELD)] * 5


async def test_blind_a_run_that_cannot_park_is_held_too() -> None:
    ran: list[int] = []

    def ping(n: int) -> str:
        ran.append(n)
        return "pong"

    calls: list[tuple[str, dict[str, Any]]] = [
        ("ping", {"n": n}) for n in range(SPEND_READ_FAILURES_TO_ARM + 3)
    ]
    guard, output = await _unparked(calls, [Tool(ping)], spend=_Spend(None))
    assert output == "end" and guard.budget_armed
    assert ran == list(range(SPEND_READ_FAILURES_TO_ARM - 1)), "the calls before the limit ran, none after"


async def test_at_the_cap_a_run_that_cannot_park_may_still_publish() -> None:
    published: list[str] = []

    def ping(n: int) -> str:
        return "pong"

    def publish(path: str) -> None:
        published.append(path)

    calls: list[tuple[str, dict[str, Any]]] = [("ping", {"n": 1}), (PUBLISH, {"path": "/work/map.json"})]
    await _unparked(calls, [Tool(ping), Tool(publish, name=PUBLISH)], spend=_Spend(_AT_CAP))
    assert published == ["/work/map.json"]


async def test_a_call_the_budget_held_is_not_replayed() -> None:
    def ping(x: int) -> str:
        return "pong"

    history: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart("go")])]
    for n, answer in enumerate([ToolReturn("pong"), not_run(BUDGET_FINISH)]):
        history.append(ModelResponse(parts=[ToolCallPart("ping", {"x": 1}, tool_call_id=f"h{n}")]))
        part = ToolReturnPart("ping", answer.return_value, tool_call_id=f"h{n}", metadata=answer.metadata)
        history.append(ModelRequest(parts=[part]))
    guard, runs = watched(can_park=True, outcome=False)
    agent = Agent(
        _scripted([("ping", {"x": 1})]),
        deps_type=TaskDeps,
        output_type=str,
        toolsets=[FunctionToolset([ping])],
        capabilities=[guard],
    )
    await agent.run("go on", message_history=history, deps=TaskDeps())
    assert runs[0].stalls == 0, "one ping ran before and one now: the held one met nothing"


# ── the same search, retyped ─────────────────────────────────────────────────


def _search() -> Tool[TaskDeps]:
    def web_search(queries: list[str], n: int = 0) -> str:
        return "results"

    return Tool(web_search)


async def test_the_same_search_in_another_case_or_spacing_is_a_repeat() -> None:
    queries = ["acme pricing 2026", "Acme pricing 2026", "  acme   PRICING\t2026 "]
    calls: list[tuple[str, dict[str, Any]]] = [("web_search", {"queries": [q]}) for q in queries]
    guard, _ = await _unparked(calls, [_search()])
    assert guard.stalls == 1


async def test_the_same_batch_in_another_order_is_a_repeat() -> None:
    batches = [
        ["acme pricing", "acme reviews"],
        ["Acme Reviews", "acme pricing"],
        ["acme  reviews", "ACME pricing"],
    ]
    guard, _ = await _unparked([("web_search", {"queries": b}) for b in batches], [_search()])
    assert guard.stalls == 1


async def test_searches_that_differ_in_words_or_count_are_not_a_repeat() -> None:
    calls: list[tuple[str, dict[str, Any]]] = [
        ("web_search", {"queries": ["acme pricing 2026"]}),
        ("web_search", {"queries": ["acme pricing 2026", "acme pricing 2025"]}),
        ("web_search", {"queries": ["acme pricing 2026"], "n": 5}),
    ]
    guard, _ = await _unparked(calls, [_search()])
    assert guard.stalls == 0


async def test_another_tools_arguments_stay_exact() -> None:
    """A shell command or a typed text means what its case and spacing say."""

    def shell(command: str) -> str:
        return "ok"

    commands = ["cat README", "cat readme", "cat  README"]
    guard, _ = await _unparked([("shell", {"command": c}) for c in commands], [Tool(shell)])
    assert guard.stalls == 0


# ── the notices tell a run that cannot park what the cap will do ─────────────


async def test_a_run_that_cannot_park_is_never_promised_a_pause() -> None:
    told: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        told.extend(
            str(p.content)
            for m in messages
            if isinstance(m, ModelRequest)
            for p in m.parts
            if isinstance(p, UserPromptPart)
        )
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("ping", {})])
        return ModelResponse(parts=[TextPart("end")])

    def ping() -> str:
        return "pong"

    half = BudgetFigures(ceiling_usd=Decimal("1.00"), spent_usd=Decimal("0.55"))
    agent = Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        output_type=str,
        toolsets=[FunctionToolset([ping])],
        capabilities=[RunGuard(can_park=False, outcome=False)],
    )
    await agent.run("go", deps=TaskDeps(spend=_Spend(half)))
    (notice,) = {t for t in told if "half of this task's budget" in t}
    assert "pauses and asks" not in notice
    assert "finish with what you have" in notice and "keep working" in notice
