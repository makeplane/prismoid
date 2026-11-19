# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A step stops at its next safe point once its task has ended (failed or cancelled).

When one writer of a parallel wave raises, the wave fails the task and `durable.cancel`s its
sibling - but a DBOS cancel only lands at the child workflow's next durable step, and a step's
model run has none, so the sibling could run on after `task.status{failed}`: model calls, a
published artifact, `step.state done`, all on a dead task. The safe points read the task row each
time (`StepInbox.pending`), and also read that it ended."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.capabilities.steering import Steering
from prismoid.ai.deps import TaskDeps
from prismoid.errors import ModelIncapable
from prismoid.schemas.steering import SafePoint, SteerPending


@dataclass
class EndingInbox:
    """The task ends after `reads_alive` safe-point reads."""

    reads_alive: int
    told: int = 1
    park_pause: bool = True
    reads: int = 0

    async def pending(self, after_epoch: int) -> SteerPending:
        self.reads += 1
        return SteerPending(ended=self.reads > self.reads_alive)

    async def applied(self, steers: Any, point: SafePoint) -> None:
        raise AssertionError("nothing to apply")

    async def paused_here(self, point: SafePoint) -> None:
        raise AssertionError("nothing pauses")


async def _run(reads_alive: int) -> tuple[list[str], int]:
    ran: list[str] = []
    calls: list[int] = []

    def publish() -> str:
        ran.append("publish")
        return "published"

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(1)
        if len(calls) == 1:
            return ModelResponse(parts=[ToolCallPart("publish", {}, tool_call_id="p1")])
        return ModelResponse(parts=[TextPart("done")])

    agent: Agent[TaskDeps, str] = Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        toolsets=[FunctionToolset([publish])],
        capabilities=[Steering(EndingInbox(reads_alive))],
    )
    with pytest.raises(ModelIncapable) as stopped:
        await agent.run("write the FAQ", deps=TaskDeps())
    assert stopped.value.context.get("infra") is True  # not the model's fault: an infra rung
    return ran, len(calls)


async def test_a_task_that_ended_before_the_first_request_buys_no_model_call() -> None:
    assert await _run(reads_alive=0) == ([], 0)


async def test_a_task_that_ended_mid_run_never_runs_the_next_tool() -> None:
    # read 1: before the first request (alive); read 2: before `publish` (ended).
    assert await _run(reads_alive=1) == ([], 1)


def test_a_live_task_reads_as_not_ended() -> None:
    assert SteerPending().ended is False
