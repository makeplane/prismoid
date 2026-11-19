# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Money stops a run in one place: the task's budget, read at every safe point. A plan's estimate is the
figure a person is shown, never a ceiling on a running step; runaway loops meet the request ceiling.

A live pass on a cheap model: a maker step spent $0.167 against twice its token estimate (~$0.16) and was
held from writing, so it published nothing though the task's budget had room."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal

import pytest
from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.capabilities.lead_check import sized
from prismoid.ai.capabilities.loops import RunGuard
from prismoid.ai.deps import TaskDeps
from prismoid.ai.rules.budget import Envelope
from prismoid.ai.tools.frame_tools import PUBLISH
from prismoid.schemas.lead import LeadFacts, SubmitPlan, TurnOrigin
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.plan import BudgetFigures, PlanFacts, Seat, StepSpec
from prismoid.schemas.steering import BUDGET_METADATA
from prismoid.schemas.usage import Unit

_HAIKU = {
    Unit.INPUT_TOKEN: Decimal("0.000001"),
    Unit.OUTPUT_TOKEN: Decimal("0.000005"),
    Unit.CACHE_READ_TOKEN: Decimal("0.0000001"),
    Unit.CACHE_WRITE_TOKEN: Decimal("0.00000125"),
}
_MAKER = Seat(
    name="maker",
    model_class=ModelClass.FAST_EXECUTOR,
    seat_class=ModelClass.FAST_EXECUTOR,
    tools=("publish", "shell", "write_file"),
)
_KNOWN = PlanFacts(prices={ModelClass.FAST_EXECUTOR: _HAIKU}, seats=(_MAKER,))


def _cheap_maker_plan() -> SubmitPlan:
    step = {
        "label": "s1",
        "agent": "maker",
        "title": "Build it",
        "goal": "Build the workbook.",
        "publishes": True,
    }
    ending = SubmitPlan.model_validate({"say": "On it.", "plan": {"deliverable": "files", "steps": [step]}})
    return ending.model_copy(
        update={"plan": sized(ending.plan, LeadFacts(turn=1, origin=TurnOrigin.WEB), _KNOWN)}
    )


def _maker(writes: int) -> FunctionModel:
    """Writes `writes` files, publishes, then answers."""

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        made = sum(1 for m in messages if isinstance(m, ModelResponse))
        if made < writes:
            return ModelResponse(parts=[ToolCallPart("write_file", {"n": made}, tool_call_id=f"w{made}")])
        if made == writes:
            return ModelResponse(parts=[ToolCallPart(PUBLISH, {}, tool_call_id="pub")])
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(model)


def _agent(
    model: FunctionModel, ran: list[str], guard: RunGuard
) -> Agent[TaskDeps, str | DeferredToolRequests]:
    def write_file(n: int) -> str:
        ran.append(f"write {n}")
        return "written"

    def publish() -> str:
        ran.append(PUBLISH)
        return "published"

    return Agent(
        model,
        deps_type=TaskDeps,
        output_type=[str, DeferredToolRequests],
        toolsets=[FunctionToolset([write_file, publish])],
        capabilities=[guard],
    )


@pytest.mark.parametrize("budget_only", [True, False])
async def test_a_cheap_step_past_twice_its_estimate_finishes_and_publishes_inside_the_task_budget(
    budget_only: bool,
) -> None:
    plan = _cheap_maker_plan()
    assert plan.plan.estimate is not None
    estimate = plan.plan.estimate.usd
    step_spent = estimate * 2 + Decimal("0.007")  # past the old guessed ceiling, as live
    task = BudgetFigures(ceiling_usd=Decimal("2.00"), spent_usd=step_spent)

    async def spend() -> BudgetFigures:
        return task

    ran: list[str] = []
    guard = RunGuard(budget_only=budget_only, can_park=not budget_only, outcome=False)
    result = await _agent(_maker(3), ran, guard).run("go", deps=TaskDeps(spend=spend))
    assert result.output == "done"
    assert ran == ["write 0", "write 1", "write 2", PUBLISH], "every write ran and the file was published"


def test_a_stored_step_that_carries_the_old_step_ceiling_still_reads() -> None:
    old = {"usd": "0.08", "ceiling_usd": "0.16", "requests": 10}
    spec = StepSpec.model_validate({"agent": "maker", "envelope": old})
    assert spec.agent == "maker" and "envelope" not in spec.model_dump()
    step = {"label": "s1", "agent": "maker", "title": "Build it", "goal": "Build it.", "envelope": old}
    plan = SubmitPlan.model_validate({"say": "On it.", "plan": {"deliverable": "files", "steps": [step]}})
    assert "envelope" not in plan.plan.steps[0].model_dump()


async def test_the_task_budget_stops_a_run_at_the_safe_point_it_is_reached() -> None:
    """Reads under the cap, then at it: the calls before ran, the next one parks on the budget gate.
    Each call is read twice - at its request and again at its dispatch."""
    reads = iter([Decimal("0.50"), Decimal("0.50"), Decimal("0.90"), Decimal("0.90"), Decimal("1.00")])

    async def spend() -> BudgetFigures:
        return BudgetFigures(ceiling_usd=Decimal("1.00"), spent_usd=next(reads, Decimal("1.00")))

    ran: list[str] = []
    result = await _agent(_maker(5), ran, RunGuard(can_park=True, outcome=False)).run(
        "go", deps=TaskDeps(spend=spend)
    )
    assert ran == ["write 0", "write 1"]
    assert isinstance(result.output, DeferredToolRequests)
    assert [result.output.metadata[c.tool_call_id] for c in result.output.calls] == [BUDGET_METADATA]


def _looping(name: str) -> FunctionModel:
    """Never ends on its own: a `ping` every request."""

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        made = sum(1 for m in messages if isinstance(m, ModelResponse))
        return ModelResponse(parts=[ToolCallPart("ping", {}, tool_call_id=f"{name}{made}")])

    return FunctionModel(model)


async def _seat(
    name: str, spend: Callable[[], Awaitable[BudgetFigures]], requests: int | None, ran: list[str]
) -> str:
    def ping() -> str:
        ran.append(name)
        return "pong"

    agent: Agent[TaskDeps, str] = Agent(
        _looping(name),
        deps_type=TaskDeps,
        output_type=str,
        toolsets=[FunctionToolset([ping])],
        capabilities=[RunGuard(budget_only=True)],
    )
    limits = Envelope(requests=requests, wall_s=60).limits()
    try:
        return (await agent.run("go", deps=TaskDeps(spend=spend), usage_limits=limits)).output
    except UsageLimitExceeded:
        return "ceiling"


async def test_parallel_seats_each_meet_their_own_request_ceiling() -> None:
    async def roomy() -> BudgetFigures:
        return BudgetFigures(ceiling_usd=Decimal("100"), spent_usd=Decimal("0"))

    ran: list[str] = []
    ended = await asyncio.gather(_seat("a", roomy, 4, ran), _seat("b", roomy, 6, ran))
    assert ended == ["ceiling", "ceiling"]
    assert (ran.count("a"), ran.count("b")) == (4, 6), "each seat runs to its own ceiling, not the other's"


async def test_parallel_seats_share_one_task_budget() -> None:
    """Both seats read the task's one spend: once it reaches the cap, neither runs another call."""
    reads = 0

    async def shared() -> BudgetFigures:
        nonlocal reads
        reads += 1
        return BudgetFigures(
            ceiling_usd=Decimal("1.00"), spent_usd=Decimal("1.00") if reads > 4 else Decimal("0")
        )

    ran: list[str] = []
    await asyncio.gather(_seat("a", shared, 20, ran), _seat("b", shared, 20, ran))
    assert 0 < len(ran) <= 4, "the calls before the shared cap ran; none after it"
