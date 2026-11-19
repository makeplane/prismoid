# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""An unguarded step agent (a gather seat, a section writer) reads its task's spend too: the run
guard's budget-only mode. At the cap - or blind, after `SPEND_READ_FAILURES_TO_ARM` failed reads -
every call but a way to finish is held and the run ends with what it has. Nothing else of the
guard rides along: no budget notices, no stall brake, and never a park (its output type has
nowhere to put one). Before, such a run spent on past the cap to its request limit."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.agents.base import AgentDef, definition_for
from prismoid.ai.agents.mounts import NOTHING, Mount, run_guard
from prismoid.ai.capabilities.loops import SPEND_READ_FAILURES_TO_ARM, RunGuard
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.meter import MeterContext
from prismoid.schemas.guard import BUDGET_FINISH
from prismoid.schemas.plan import BudgetFigures

_AT_CAP = BudgetFigures(ceiling_usd=Decimal("1.00"), spent_usd=Decimal("1.00"))
_MOST = BudgetFigures(ceiling_usd=Decimal("1.00"), spent_usd=Decimal("0.80"))


class _Spend:
    def __init__(self, figures: BudgetFigures | None) -> None:
        self.figures = figures

    async def __call__(self) -> BudgetFigures:
        if self.figures is None:
            raise RuntimeError("db outage")
        return self.figures


async def _run(spend: _Spend, calls: int = 10) -> tuple[list[str], list[str]]:
    """The same `ping` `calls` times, then an answer: what ran, and every line the model was sent."""
    ran: list[str] = []
    heard: list[str] = []

    def ping() -> str:
        ran.append("ping")
        return "pong"

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        heard.extend(
            str(getattr(part, "content", ""))
            for m in messages
            if isinstance(m, ModelRequest)
            for part in m.parts
        )
        made = sum(1 for m in messages if isinstance(m, ModelResponse))
        if made < calls:
            return ModelResponse(parts=[ToolCallPart("ping", {}, tool_call_id=f"p{made}")])
        return ModelResponse(parts=[TextPart("done")])

    agent: Agent[TaskDeps, str] = Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        output_type=str,  # a gather seat's: no `DeferredToolRequests`, a park has nowhere to go
        toolsets=[FunctionToolset([ping])],
        capabilities=[RunGuard(budget_only=True)],
    )
    assert (await agent.run("go", deps=TaskDeps(spend=spend))).output == "done"
    return ran, heard


async def test_at_the_cap_every_call_is_held_and_the_run_ends_with_what_it_has() -> None:
    ran, heard = await _run(_Spend(_AT_CAP))
    assert ran == []
    assert BUDGET_FINISH in heard


async def test_a_blind_guard_holds_after_the_limit_of_failed_reads() -> None:
    ran, _ = await _run(_Spend(None))
    assert len(ran) == SPEND_READ_FAILURES_TO_ARM - 1  # the reads before it armed were a blip


async def test_under_the_cap_it_neither_talks_nor_brakes_a_repeat() -> None:
    ran, heard = await _run(_Spend(_MOST))
    assert len(ran) == 10  # the same call ten times: no stall brake in this mode
    assert not any("budget" in line for line in heard)  # no 50/75% notice


def _mount(definition: AgentDef, deps: TaskDeps) -> Mount:
    return Mount(
        definition=definition,
        deps=deps,
        catalog=[],
        meter=MeterContext(),
        served=None,
        order=None,
        quality=None,
    )


def _guard_of(definition: AgentDef, deps: TaskDeps) -> RunGuard | None:
    mounted = run_guard(_mount(definition, deps))
    if mounted is NOTHING:
        return None
    (guard,) = mounted.capabilities
    assert isinstance(guard, RunGuard)
    return guard


def test_an_unguarded_step_agent_mounts_the_budget_only_guard() -> None:
    spend: Any = _Spend(_MOST)
    seat = definition_for("researcher", TaskDeps(lane_role="gather"))
    assert not seat.guarded
    guard = _guard_of(seat, TaskDeps(spend=spend))
    assert guard is not None and guard.budget_only and not guard.can_park


def test_nothing_mounts_where_no_spend_is_wired() -> None:
    """Judges, the planner, chat: only the step path wires `deps.spend`."""
    assert _guard_of(definition_for("researcher", TaskDeps(lane_role="gather")), TaskDeps()) is None


def test_a_guarded_agent_keeps_the_whole_guard() -> None:
    spend: Any = _Spend(_MOST)
    operator = definition_for("operator", TaskDeps())
    guard = _guard_of(operator, TaskDeps(spend=spend))
    assert guard is not None and not guard.budget_only and guard.can_park
