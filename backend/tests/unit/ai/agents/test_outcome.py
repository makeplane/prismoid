# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The typed ending: `Outcome` and `Proof`, the operator's `done`
output tool, a finished run continued with one more user line, and the step frame's outcome."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError
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

import prismoid.ai.agents.base as agents_base
from prismoid.ai.agents.base import build
from prismoid.ai.agents.messages import continue_with
from prismoid.ai.agents.run import Continue
from prismoid.ai.capabilities.durability import CallLogScope
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.schemas.events import StepStateChanged
from prismoid.schemas.ids import StepId, TaskId
from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.outputs import DONE_TOOL, Outcome, Proof, output_tool_name
from prismoid.schemas.plan import StepState
from prismoid.schemas.usage import Unit
from tests.support.rigs.durability_drill import MemoryLog


def _catalog() -> list[ModelSpec]:
    return [
        ModelSpec(
            provider="fake",
            model_id="m1",
            family=Family("fake"),
            protocol="openai_compatible",
            classes=(ModelClass.FAST_EXECUTOR,),
            base_url="https://example.invalid/v1",
            prices={Unit.INPUT_TOKEN: Decimal("0")},
        )
    ]


def _user_lines(message: ModelMessage) -> list[str]:
    return [str(p.content) for p in message.parts if isinstance(p, UserPromptPart)]


def test_an_outcome_is_typed_and_bounded() -> None:
    blocked = Outcome(status="blocked", summary="The admin needs a sign-in.", blocked_by="login")
    assert blocked.proof is None
    assert Proof(path="/work/hello.txt").url is None
    with pytest.raises(ValidationError):
        Outcome(status="maybe", summary="x")  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValidationError):
        Outcome(status="done", summary="x" * 2001)
    with pytest.raises(ValidationError):
        Outcome(status="done", summary="")


def test_only_the_outcome_names_its_output_tool() -> None:
    assert output_tool_name("outcome") == DONE_TOOL == "done"
    assert output_tool_name(None) is None
    assert output_tool_name("collection_result") is None, "every other typed output keeps final_result"


async def test_the_computer_agent_finishes_with_done(monkeypatch: pytest.MonkeyPatch) -> None:
    offered: list[list[str]] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        offered.append([t.name for t in info.output_tools])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    DONE_TOOL,
                    {"status": "done", "summary": "Opened plane.so.", "proof": {"url": "https://plane.so"}},
                )
            ]
        )

    monkeypatch.setattr(agents_base, "resolve", lambda *a, **k: FunctionModel(model))
    result = await build("operator", TaskDeps(), _catalog()).run("open plane.so")
    assert offered == [["done"]], "one output tool, named so the prompt can say it"
    assert result.output == Outcome(
        status="done", summary="Opened plane.so.", proof=Proof(url="https://plane.so")
    )


def test_continue_with_appends_to_a_trailing_request_and_opens_one_after_a_response() -> None:
    ended: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart("go")]),
        ModelResponse(parts=[ToolCallPart(DONE_TOOL, {"status": "done", "summary": "x"}, tool_call_id="d1")]),
        ModelRequest(parts=[ToolReturnPart(DONE_TOOL, "Final result processed.", tool_call_id="d1")]),
    ]
    out = continue_with(ended, "check again")
    assert len(out) == 3, "two requests in a row are not a conversation every provider takes"
    assert _user_lines(out[-1]) == ["check again"]
    assert len(ended[-1].parts) == 1, "the saved history is never mutated"

    talked: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart("go")]),
        ModelResponse(parts=[TextPart("hi")]),
    ]
    out = continue_with(talked, "more")
    assert len(out) == 3 and isinstance(out[-1], ModelRequest) and _user_lines(out[-1]) == ["more"]


async def test_a_finished_run_continues_with_its_whole_history(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[list[ModelMessage]] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        requests.append(list(messages))
        return ModelResponse(
            parts=[ToolCallPart(DONE_TOOL, {"status": "done", "summary": f"turn {len(requests)}"})]
        )

    monkeypatch.setattr(agents_base, "resolve", lambda *a, **k: FunctionModel(model))
    runner = build("operator", TaskDeps(), _catalog())
    log = await CallLogScope.open(MemoryLog(), StepId(uuid.uuid4()), fresh=True)
    await runner.run("open it", log=log)
    second = await runner.run(Continue("attempt:2", "check the page and finish again"), log=log)
    assert isinstance(second.output, Outcome) and second.output.summary == "turn 2"
    assert len(requests[1]) > len(requests[0]), "the same conversation, carried"
    assert "check the page and finish again" in _user_lines(requests[1][-1])


def test_the_step_frame_carries_an_optional_outcome() -> None:
    base = {"seq": 1, "task_id": TaskId(uuid.uuid4()), "at": dt.datetime.now(dt.UTC), "state": StepState.DONE}
    assert StepStateChanged(**base).outcome is None, "a frame from before this round reads as before"  # pyright: ignore[reportArgumentType]
    frame = StepStateChanged(**base, outcome="blocked", blocked_by="login")  # pyright: ignore[reportArgumentType]
    again = StepStateChanged.model_validate_json(frame.model_dump_json())
    assert again.outcome == "blocked" and again.blocked_by == "login"


async def test_a_continued_run_needs_its_call_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agents_base, "resolve", lambda *a, **k: FunctionModel(lambda m, i: ModelResponse(parts=[]))
    )
    with pytest.raises(TypeError):
        await build("operator", TaskDeps(), _catalog()).run(Continue("attempt:2", "go on"))
