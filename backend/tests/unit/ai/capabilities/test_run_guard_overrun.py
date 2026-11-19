# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The budget brake must not run past the cap. The guard decided at the model request only, so the
calls of one response (and a priced call's own spend) all ran on the request's figures; and a model
request is never refused at the cap, so concurrent seats each pass one check. The first is fixed (the
spend is read again at each dispatch); the second needs a reservation before the call (proposed)."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.capabilities.loops import RunGuard
from prismoid.ai.deps import TaskDeps
from prismoid.schemas.plan import BudgetFigures

CAP = Decimal("0.10")
PER_REQUEST = Decimal("0.05")
PER_SEARCH = Decimal("0.04")


class _Ledger:
    """The task's ledger: every model request bills PER_REQUEST once it returns."""

    def __init__(self, spent: Decimal) -> None:
        self.spent = spent
        self.requests = 0

    async def read(self) -> BudgetFigures:
        return BudgetFigures(ceiling_usd=CAP, spent_usd=self.spent)


def _seat(ledger: _Ledger, *, obeys: bool = False) -> Agent[TaskDeps, str]:
    def ping() -> str:
        return "pong"

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(0.01)  # the provider's latency: siblings interleave here
        ledger.requests += 1
        ledger.spent += PER_REQUEST  # billed after the call, as the meter does
        made = sum(1 for m in messages if isinstance(m, ModelResponse))
        told_to_finish = obeys and "budget" in str(messages[-1]).lower()
        if made < 3 and not told_to_finish:
            return ModelResponse(parts=[ToolCallPart("ping", {}, tool_call_id=f"p{made}")])
        return ModelResponse(parts=[TextPart("done")])

    return Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        output_type=str,
        toolsets=[FunctionToolset([ping])],
        capabilities=[RunGuard(budget_only=True)],
    )


async def test_a_priced_call_that_reaches_the_cap_holds_the_next_call_of_its_batch() -> None:
    """One response, two web searches: the first bills the task past its cap, so the second never runs."""
    ledger = _Ledger(Decimal("0.07"))
    ran: list[str] = []

    async def search(q: str) -> str:
        ran.append(q)
        ledger.spent += PER_SEARCH
        return "results"

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if sum(1 for m in messages if isinstance(m, ModelResponse)) == 0:
            return ModelResponse(
                parts=[
                    ToolCallPart("search", {"q": "a"}, tool_call_id="s1"),
                    ToolCallPart("search", {"q": "b"}, tool_call_id="s2"),
                ]
            )
        return ModelResponse(parts=[TextPart("done")])

    agent: Agent[TaskDeps, str] = Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        output_type=str,
        toolsets=[FunctionToolset([search], sequential=True)],
        capabilities=[RunGuard(budget_only=True)],
    )
    await agent.run("go", deps=TaskDeps(spend=ledger.read))
    assert ran == ["a"], f"ran {ran}; spent {ledger.spent} against {CAP}"


@pytest.mark.xfail(strict=True, reason="a request is never refused at the cap: needs a pre-call reservation")
async def test_a_run_already_at_the_cap_still_buys_model_requests() -> None:
    ledger = _Ledger(CAP)  # spent == cap before the run starts
    await _seat(ledger, obeys=True).run("go", deps=TaskDeps(spend=ledger.read))
    assert ledger.requests == 0, f"{ledger.requests} model requests bought at the cap; spent {ledger.spent}"


@pytest.mark.xfail(strict=True, reason="concurrent seats each pass one check: needs a pre-call reservation")
async def test_concurrent_seats_overrun_the_cap_by_more_than_one_call() -> None:
    ledger = _Ledger(Decimal("0.09"))  # one cent under the cap
    seats = [_seat(ledger, obeys=True) for _ in range(4)]
    await asyncio.gather(*(s.run("go", deps=TaskDeps(spend=ledger.read)) for s in seats))
    assert ledger.spent <= CAP + PER_REQUEST, f"spent {ledger.spent} against a cap of {CAP}"
