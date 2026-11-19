# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The run guard on a run that continues a history: a resumed park replays the calls the history
already answered (the stall is the step's)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, cast

import pytest
from pydantic_ai import Agent, Tool, ToolReturn
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.capabilities.loops import RunGuard
from prismoid.ai.deps import TaskDeps
from prismoid.ai.tools.browser import PAGE_RESET
from prismoid.ai.tools.call_rows import not_run
from prismoid.schemas.guard import BUDGET_RESUMED, FINISH_NOW, stall_notice
from tests.support.guard import browser_page, watched

Outcome = Literal["success", "retried"]


def _told(messages: list[ModelMessage]) -> str:
    return "\n".join(
        str(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    )


def _answered(calls: list[tuple[str, dict[str, Any], str | ToolReturn[str]]]) -> list[ModelMessage]:
    """A parked run's history as the store gives it back: the prompt, then each call and its answer,
    in order (a browser answer's facts come back as a dict)."""
    history: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart("go")])]
    for n, (name, args, answer) in enumerate(calls):
        content, facts = (
            (answer.return_value, answer.metadata) if isinstance(answer, ToolReturn) else (answer, None)
        )
        history.append(ModelResponse(parts=[ToolCallPart(name, args, tool_call_id=f"h{n}")]))
        history.append(
            ModelRequest(parts=[ToolReturnPart(name, content, tool_call_id=f"h{n}", metadata=facts)])
        )
    return ModelMessagesTypeAdapter.validate_json(ModelMessagesTypeAdapter.dump_json(history))


async def _resumed(
    history: list[ModelMessage],
    calls: list[tuple[str, dict[str, Any]]],
    tools: list[Tool[TaskDeps]],
    deps: TaskDeps | None = None,
) -> tuple[RunGuard, list[str]]:
    """The run after the park: a new guard, the saved history, then `calls` live, then an ending."""
    seen: list[str] = []
    asked = 0

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal asked
        seen.append(_told(messages))
        asked += 1
        if asked <= len(calls):
            name, args = calls[asked - 1]
            return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id=f"c{asked}")])
        return ModelResponse(parts=[TextPart("end")])

    guard, runs = watched(can_park=True)
    agent = Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        output_type=str,
        toolsets=[FunctionToolset(tools)],
        capabilities=[guard],
    )
    await agent.run("go on", message_history=history, deps=deps or TaskDeps())
    return runs[0], seen


STUCK = browser_page("Export not started")


def _stuck(ref: str = "") -> ToolReturn[str]:
    return STUCK


async def test_clicks_before_a_stop_count_after_it() -> None:
    """Clicks on an unchanged page before the run stopped still count in the run that continues it (no
    person's answer between: `RECALL_FROM` unset)."""
    clicks: list[tuple[str, dict[str, Any], str | ToolReturn[str]]] = [
        ("browser_click", {"ref": f"e1.{n}"}, STUCK) for n in range(4)
    ]
    history = _answered([*clicks, ("browser_handoff", {"reason": "click Export"}, "Nobody took over.")])
    click = [("browser_click", {"ref": "e1.4"})]
    guard, seen = await _resumed(history, click, [Tool(_stuck, name="browser_click")])
    assert guard.stalls == 1
    assert "You are repeating `browser_click` without effect" in seen[-1]


async def test_a_failed_act_in_the_history_keeps_the_clicks_around_it() -> None:
    """An act the sidecar refused carries its check and no page facts: it neither counts nor costs
    the run the clicks the history holds around it."""
    failed = ToolReturn("click failed", metadata={"act": {"verb": "click", "ok": False}})
    clicks: list[tuple[str, dict[str, Any], str | ToolReturn[str]]] = [
        ("browser_click", {"ref": f"e1.{n}"}, failed if n == 2 else STUCK) for n in range(5)
    ]
    click = [("browser_click", {"ref": "e1.5"})]
    guard, _ = await _resumed(_answered(clicks), click, [Tool(_stuck, name="browser_click")])
    assert guard.stalls == 1


async def test_a_fresh_run_has_no_history_to_count() -> None:
    guard, _ = await _resumed([], [("browser_click", {"ref": "e1.1"})], [Tool(_stuck, name="browser_click")])
    assert guard.stalls == 0


@pytest.mark.parametrize(
    ("answer", "stalls"),
    [
        ("pong", 1),
        (FINISH_NOW, 1),  # the words alone are no mark: a tool may say anything
        (not_run(FINISH_NOW), 0),
        (not_run(BUDGET_RESUMED), 0),
        (not_run(f"{PAGE_RESET}\n\n{BUDGET_RESUMED}"), 0),  # a resume after the Machine slept
    ],
)
async def test_a_call_that_never_ran_is_not_replayed(answer: str | ToolReturn[str], stalls: int) -> None:
    """A call the guard held or the budget did not run never met anything: it is no repeat."""

    def ping(x: int) -> str:
        return "pong"

    history = _answered([("ping", {"x": 1}, "pong"), ("ping", {"x": 1}, answer)])
    guard, _ = await _resumed(history, [("ping", {"x": 1})], [Tool(ping)])
    assert guard.stalls == stalls


def _sent_back(outcome: Outcome) -> list[ModelMessage]:
    """The same click three times, each answered with `outcome`: sent back, it never ran."""
    history: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart("go")])]
    for n in range(3):
        call = ToolCallPart("browser_click", {"ref": "e1.1"}, tool_call_id=f"r{n}")
        history.append(ModelResponse(parts=[call]))
        history.append(
            ModelRequest(
                parts=[
                    ToolReturnPart("browser_click", "no such element", tool_call_id=f"r{n}", outcome=outcome)
                ]
            )
        )
    return history


def _id_reused(outcome: Outcome) -> list[ModelMessage]:
    """Five clicks on an unchanged page; the first answered with `outcome`, its id reused by the second."""
    history = _answered([("browser_click", {"ref": f"e1.{n}"}, STUCK) for n in range(5)])
    first, second = cast(ModelRequest, history[2]).parts[0], cast(ModelRequest, history[4]).parts[0]
    assert isinstance(first, ToolReturnPart) and isinstance(second, ToolReturnPart)
    first.outcome = outcome
    cast(ToolCallPart, history[3].parts[0]).tool_call_id = second.tool_call_id = first.tool_call_id
    return history


@pytest.mark.parametrize("history", [_sent_back, _id_reused])
@pytest.mark.parametrize(("outcome", "stalls"), [("success", 1), ("retried", 0)])
async def test_a_retried_return_is_not_a_call_that_ran(
    history: Callable[[Outcome], list[ModelMessage]], outcome: Outcome, stalls: int
) -> None:
    """A call the model was sent back from (a refusal, a bad argument) is no repeat and met no page."""
    guard, _ = await _resumed(history(outcome), [], [Tool(_stuck, name="browser_click")])
    assert guard.stalls == stalls


async def test_what_the_history_already_said_is_not_said_again() -> None:
    """A stall the parked run met was told then: the resumed run carries its count, not its words."""
    history = _answered([("browser_click", {"ref": "e1.1"}, STUCK)] * 5)
    history.append(ModelRequest(parts=[UserPromptPart(stall_notice("browser_click"))]))
    history.append(ModelResponse(parts=[TextPart("trying something else")]))
    guard, seen = await _resumed(history, [("browser_snapshot", {})], [Tool(_stuck, name="browser_snapshot")])
    assert guard.stalls == 1 and not guard.finish_now
    assert all(s.count("You are repeating") == 1 for s in seen)
