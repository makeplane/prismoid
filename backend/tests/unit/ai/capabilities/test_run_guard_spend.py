# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The run guard fails closed when it cannot see the task's spend: one or two failed reads are a
blip (the safe point is skipped), a run of `SPEND_READ_FAILURES_TO_ARM` arms the budget park, and
the next good read hands the decision back to the figures."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.capabilities.loops import SPEND_READ_FAILURES_TO_ARM, RunGuard
from prismoid.ai.deps import TaskDeps
from prismoid.schemas.guard import ALLOWANCE_FINISH, BUDGET_FINISH
from prismoid.schemas.plan import BudgetFigures
from prismoid.schemas.steering import BUDGET_METADATA

_UNDER = BudgetFigures(ceiling_usd=Decimal("1.00"), spent_usd=Decimal("0.10"))
# A workspace whose allowance is spent (ADR-103), on a task with no cap of its own, and the same task
# once it is back: under an allowance every task reads figures, so its return can disarm.
_EXHAUSTED = BudgetFigures(ceiling_usd=Decimal(0), spent_usd=Decimal(0), exhausted=True)
_TOPPED_UP = BudgetFigures(ceiling_usd=Decimal(0), spent_usd=Decimal(0))


@dataclass
class ScriptedSpend:
    """One entry per read: figures, or None for a read that raises. The last entry repeats."""

    script: list[BudgetFigures | None]
    reads: int = field(default=0, init=False)

    async def __call__(self) -> BudgetFigures | None:
        self.reads += 1
        answer = self.script[min(self.reads, len(self.script)) - 1]
        if answer is None:
            raise RuntimeError("db outage")
        return answer


def _pinging(calls: int) -> FunctionModel:
    """A model that calls `ping` `calls` times, then answers."""

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        made = sum(1 for m in messages if isinstance(m, ModelResponse))
        if made < calls:
            return ModelResponse(parts=[ToolCallPart("ping", {}, tool_call_id=f"p{made}")])
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(model)


async def _run(calls: int, spend: ScriptedSpend) -> tuple[str | DeferredToolRequests, list[str]]:
    ran: list[str] = []

    def ping() -> str:
        ran.append("ping")
        return "pong"

    agent: Agent[TaskDeps, str | DeferredToolRequests] = Agent(
        _pinging(calls),
        deps_type=TaskDeps,
        output_type=[str, DeferredToolRequests],
        toolsets=[FunctionToolset([ping])],
        capabilities=[RunGuard(can_park=True)],
    )
    result = await agent.run("go", deps=TaskDeps(spend=spend))
    return result.output, ran


async def test_fewer_failed_reads_than_the_limit_only_skip_their_safe_points() -> None:
    failures: list[BudgetFigures | None] = [None] * (SPEND_READ_FAILURES_TO_ARM - 1)
    output, ran = await _run(SPEND_READ_FAILURES_TO_ARM - 1, ScriptedSpend([*failures, _UNDER]))
    assert output == "done"
    assert len(ran) == SPEND_READ_FAILURES_TO_ARM - 1


async def test_the_limit_of_failed_reads_in_a_row_parks_the_next_call_on_the_budget() -> None:
    spend = ScriptedSpend([None])  # the spend can never be read
    output, ran = await _run(SPEND_READ_FAILURES_TO_ARM + 2, spend)
    assert isinstance(output, DeferredToolRequests)
    held = output.calls[0].tool_call_id
    assert output.metadata[held] == BUDGET_METADATA
    assert len(ran) == SPEND_READ_FAILURES_TO_ARM - 1, "the calls before the limit ran, none after"
    # Each request reads, and every call reads again at its dispatch, the held one included.
    assert spend.reads == SPEND_READ_FAILURES_TO_ARM + len(ran) + 1


async def test_a_good_read_resets_the_count() -> None:
    blip: list[BudgetFigures | None] = [None] * (SPEND_READ_FAILURES_TO_ARM - 1)
    spend = ScriptedSpend([*blip, _UNDER, *blip, _UNDER])
    output, ran = await _run(2 * SPEND_READ_FAILURES_TO_ARM - 1, spend)
    assert output == "done", "two blips split by a good read never arm the park"
    assert len(ran) == 2 * SPEND_READ_FAILURES_TO_ARM - 1


async def test_sight_coming_back_disarms_a_blind_park() -> None:
    """The guard rides the agent across runs, so a second run on the same agent starts armed - and
    its first good read disarms it before any call is held."""
    ran: list[str] = []

    def ping() -> str:
        ran.append("ping")
        return "pong"

    agent: Agent[TaskDeps, str | DeferredToolRequests] = Agent(
        _pinging(SPEND_READ_FAILURES_TO_ARM),
        deps_type=TaskDeps,
        output_type=[str, DeferredToolRequests],
        toolsets=[FunctionToolset([ping])],
        capabilities=[RunGuard(can_park=True)],
    )
    blind = await agent.run("go", deps=TaskDeps(spend=ScriptedSpend([None])))
    assert isinstance(blind.output, DeferredToolRequests), "blind: the guard armed the park"
    ran.clear()
    seen = await agent.run("go", deps=TaskDeps(spend=ScriptedSpend([_UNDER])))
    assert seen.output == "done", "the figures decide again"
    assert len(ran) == SPEND_READ_FAILURES_TO_ARM


async def test_exhausted_figures_arm_the_guard() -> None:
    """A workspace with no allowance left arms the park whatever the ceiling - here there is none: the
    next call defers on the budget and never runs."""
    output, ran = await _run(2, ScriptedSpend([_EXHAUSTED]))
    assert isinstance(output, DeferredToolRequests)
    assert output.metadata[output.calls[0].tool_call_id] == BUDGET_METADATA
    assert ran == []


async def test_a_top_up_disarms_a_run_that_cannot_park() -> None:
    """An unguarded agent's budget-only guard holds every call while the workspace is out, and the
    first read after the allowance is back lets the next one run."""
    ran: list[str] = []
    told: list[str] = []

    def ping() -> str:
        ran.append("ping")
        return "pong"

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        told.extend(str(p.content) for m in messages for p in m.parts if isinstance(p, ToolReturnPart))
        made = sum(1 for m in messages if isinstance(m, ModelResponse))
        if made < 2:
            return ModelResponse(parts=[ToolCallPart("ping", {}, tool_call_id=f"p{made}")])
        return ModelResponse(parts=[TextPart("done")])

    agent: Agent[TaskDeps, str] = Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        toolsets=[FunctionToolset([ping])],
        capabilities=[RunGuard(budget_only=True)],
    )
    result = await agent.run("go", deps=TaskDeps(spend=ScriptedSpend([_EXHAUSTED, _TOPPED_UP])))
    assert result.output == "done"
    assert ALLOWANCE_FINISH in told, "the call made while out was held, told it was the allowance"
    assert BUDGET_FINISH not in told, "never blamed on a budget the task does not have"
    assert ran == ["ping"], "the call made after the allowance came back ran"
