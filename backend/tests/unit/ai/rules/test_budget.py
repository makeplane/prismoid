# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The budget envelope: the one decision on money, and a step's requests and wall.

Money: `over` is pure, so "would this overrun?" is a test, not a spent dollar; `reached` is the same
decision on a run's live figures. Requests: the ceiling is per segment, so a run that died at its
ceiling and is continued buys the ceiling plus the continuation's requests, and the replay of the
dead segment buys none. Scripted models only, over the in-memory call log."""

from __future__ import annotations

import asyncio
import time
import uuid
from decimal import Decimal
from typing import Any

import pytest
from pydantic_ai import Agent, Tool
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from prismoid.ai.agents.base import AgentDef
from prismoid.ai.agents.run import AgentRunner, Continue
from prismoid.ai.capabilities.durability import CallLogScope, PrismoidDurability
from prismoid.ai.deps import TaskDeps
from prismoid.ai.rules.budget import Envelope, over, reached
from prismoid.conf import settings
from prismoid.errors import ModelIncapable
from prismoid.schemas.ids import AgentName, StepId
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.plan import BudgetFigures
from tests.support.rigs.durability_drill import MemoryLog

D = Decimal
CEILING = 3
WRAP_UP = "You stopped at the request ceiling. Wrap up now."


def test_no_ceiling_never_constrains() -> None:
    assert over(task_limit=None, task_spent=D(999), project_limit=None, project_spent=D(999)) is None


def test_the_ceiling_is_the_last_dollar_you_may_spend() -> None:
    """`>=`, not `>`: a budget equal to what is spent has nothing left to give."""
    at = over(task_limit=D("5"), task_spent=D("5"))
    assert at is not None and at.scope == "task" and at.exhausted
    assert over(task_limit=D("5"), task_spent=D("4.99")) is None


def test_the_task_ceiling_binds_before_the_project_ceiling() -> None:
    v = over(task_limit=D("1"), task_spent=D("1"), project_limit=D("100"), project_spent=D("100"))
    assert v is not None and v.scope == "task"


def test_the_project_ceiling_stops_a_task_that_has_no_cap_of_its_own() -> None:
    v = over(task_limit=None, task_spent=D("50"), project_limit=D("10"), project_spent=D("10"))
    assert v is not None and (v.scope, v.limit) == ("project", D("10"))


def test_a_projection_reports_the_spend_and_the_coming_wave_apart() -> None:
    """The coming wave counts against the ceiling, but "spent" is only what is gone."""
    task = over(task_limit=D("10"), task_spent=D("4"), projection=D("8"))
    assert task is not None
    assert (task.scope, task.limit, task.spent, task.projected, task.exhausted) == (
        "task",
        D("10"),
        D("4"),
        D("8"),
        False,
    )
    project = over(
        task_limit=None, task_spent=D("4"), project_limit=D("100"), project_spent=D("95"), projection=D("10")
    )
    assert project is not None and (project.scope, project.spent, project.projected) == (
        "project",
        D("95"),
        D("10"),
    )


@pytest.mark.parametrize(
    ("figures", "stops"),
    [
        (None, False),
        (BudgetFigures(ceiling_usd=D(0), spent_usd=D(9)), False),
        (BudgetFigures(ceiling_usd=D("0.40"), spent_usd=D("0.39")), False),
        (BudgetFigures(ceiling_usd=D("0.40"), spent_usd=D("0.40")), True),
        (BudgetFigures(ceiling_usd=D("0.40"), spent_usd=D("1.20")), True),
        (BudgetFigures(ceiling_usd=D(0), spent_usd=D(0), exhausted=True), True),
        (BudgetFigures(ceiling_usd=D("5"), spent_usd=D("0.01"), exhausted=True), True),
    ],
)
def test_a_run_reaches_its_cap_on_its_live_figures(figures: BudgetFigures | None, stops: bool) -> None:
    """A workspace whose allowance is spent (`exhausted`) stops the run whatever its cap, and with none."""
    assert reached(figures) is stops


def test_the_workspace_is_checked_before_the_task() -> None:
    """A workspace with no allowance left stops a task whatever its own ceilings say: it is asked
    first, so a task well under its cap still parks. An allowance is a floor in its own unit, not a
    ceiling in dollars, so a wave's projection never counts against it. `workspace_left=None` (a
    deployment that sets no allowance) leaves every verdict as it was."""
    out = over(task_limit=D("5"), task_spent=D("1"), workspace_left=D(0))
    assert out is not None and out.scope == "workspace" and out.exhausted
    both = over(
        task_limit=D("1"), task_spent=D("1"), project_limit=D(1), project_spent=D(1), workspace_left=D(-10)
    )
    assert both is not None and (both.scope, both.spent, both.limit, both.projected) == (
        "workspace",
        D(10),
        D(0),
        D(0),
    ), "the floor is 0; `spent` is how far below it the workspace is"
    assert over(task_limit=D("5"), task_spent=D("1"), projection=D("9"), workspace_left=D("0.01")) is not None
    assert over(task_limit=D("5"), task_spent=D("1"), workspace_left=D("0.01")) is None

    for task_limit, task_spent, project_limit, project_spent in (
        (None, D(9), None, D(9)),
        (D(5), D(5), None, D(0)),
        (None, D(0), D(10), D(10)),
    ):
        today = over(
            task_limit=task_limit,
            task_spent=task_spent,
            project_limit=project_limit,
            project_spent=project_spent,
        )
        unenforced = over(
            task_limit=task_limit,
            task_spent=task_spent,
            project_limit=project_limit,
            project_spent=project_spent,
            workspace_left=None,
        )
        assert unenforced == today


def test_a_guarded_step_gets_the_request_ceiling_and_every_step_the_wall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.worker, "request_ceiling", 7)
    monkeypatch.setattr(settings.worker, "agent_wall_clock_s", 600)
    guarded = Envelope.for_step(guarded=True)
    assert (guarded.requests, guarded.wall_s) == (7, 600.0)
    assert Envelope.for_step(guarded=False).limits() is None, "an unguarded run keeps the library's own"
    first, second = guarded.limits(), guarded.limits()
    assert first is not None and second is not None and first is not second and first.request_limit == 7
    assert guarded.narrowed(45).wall_s == 45 and guarded.narrowed(9000).wall_s == 600


async def test_the_wall_sets_the_deadline_and_stops_the_attempt() -> None:
    deps = TaskDeps()
    before = time.monotonic()
    with pytest.raises(TimeoutError):
        await Envelope(requests=None, wall_s=0.05).within(deps, asyncio.sleep(5))
    assert deps.deadline is not None and before < deps.deadline <= time.monotonic()


class _Looks:
    """Calls a tool every request until told to wrap up, then answers; counts what the provider served."""

    def __init__(self) -> None:
        self.served = 0

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.served += 1
        told = any(
            isinstance(p, UserPromptPart) and WRAP_UP in str(p.content)
            for m in messages
            if isinstance(m, ModelRequest)
            for p in m.parts
        )
        if told:
            return ModelResponse(parts=[TextPart("done looking")])
        return ModelResponse(parts=[ToolCallPart("look", {"n": self.served}, tool_call_id=f"l{self.served}")])


def _look(n: int) -> str:
    return f"saw {n}"


def _runner(model: _Looks, step: StepId) -> AgentRunner:
    agent: Agent[TaskDeps, Any] = Agent(
        FunctionModel(model),
        name="envelope-probe",
        deps_type=TaskDeps,
        tools=[Tool(_look, name="look")],
        capabilities=[PrismoidDurability()],
    )
    definition = AgentDef(
        name=AgentName("envelope-probe"), model_class=ModelClass.CODE, description="a probe", guarded=True
    )
    return AgentRunner(agent, TaskDeps(step_id=step), definition)


async def test_a_ceiling_death_continues_and_buys_the_ceiling_plus_the_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dead segment replays from its log (nothing bought twice) and trips where it tripped; the
    continuation runs under fresh limits of its own and finishes."""
    monkeypatch.setattr(settings.worker, "request_ceiling", CEILING)
    log, model, step = MemoryLog(), _Looks(), StepId(uuid.uuid4())
    with pytest.raises(ModelIncapable) as dead:
        await _runner(model, step).run("look around", log=await CallLogScope.open(log, step, fresh=True))
    assert dead.value.pickup == "continue" and model.served == CEILING

    scope = await CallLogScope.open(log, step, fresh=False)
    result = await _runner(model, step).run(Continue("attempt:2", WRAP_UP), log=scope)

    assert result.output == "done looking" and scope.key.generation == 0
    assert model.served == CEILING + 1, "the ceiling's requests, then the continuation's one"
