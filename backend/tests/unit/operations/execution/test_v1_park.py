# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A park saved before the call log still resumes: `V1Park.of` decodes its keys, its saved conversation
loads under today's pydantic-ai, and the ids it saved answer every deferred call it ended on.

The 2.53 fixture is one such park (`tools/gen_parked_history.py`); 2.60 replaced the retry part it
holds (`"retry-prompt"`) with a retried tool return and a tool-less retry part. Any deprecation
warning from pydantic-ai fails these tests: a park must never load through a path the library is
about to remove.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from pydantic_ai import Agent, DeferredToolRequests, Tool, ToolApproved, ToolDenied
from pydantic_ai.exceptions import CallDeferred
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    RetryFeedbackPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from prismoid.ai.agents.run import FromHistory
from prismoid.ai.capabilities.compaction import COLLAPSE_HIGH_WATER, collapse_old_pages
from prismoid.ai.capabilities.durability import CallLogScope, PrismoidDurability
from prismoid.ai.deps import TaskDeps
from prismoid.ai.tools.browser import FENCE_CLOSE
from prismoid.ai.tools.browser_policy import unpaired_in
from prismoid.operations.execution.answers import answers_to, v1_deferral, v1_requests
from prismoid.operations.execution.caps import DENIED_BY_USER, ONE_BATCH_AT_A_TIME, RETRY_AFTER_ANSWER
from prismoid.operations.execution.deferrals import NOT_RUN_FOR_CONTROL
from prismoid.operations.execution.proof import quotes_shell, unsaved_edit
from prismoid.schemas.ids import StepId
from prismoid.schemas.park import ParkKind, ParkRecord, V1Park, park_of
from tests.support.paths import TESTS
from tests.support.rigs.durability_drill import MemoryLog
from tests.support.runners import runner_for

pytestmark = pytest.mark.filterwarnings("error::pydantic_ai.PydanticAIDeprecationWarning")

FIXTURE = TESTS / "fixtures" / "pydantic_ai" / "parked_history_v2_53.json"


def _park() -> dict[str, Any]:
    """The fixture's park as `resume_state` holds it: the history stored latin-1, the parked call's id."""
    with FIXTURE.open() as f:
        saved = json.load(f)
    return {"history": saved["history"], "tool_call_id": saved["tool_call_id"]}


def _loaded(v1: V1Park) -> list[ModelMessage]:
    return ModelMessagesTypeAdapter.validate_json(v1.history.encode("latin-1"))


def _decoded(state: dict[str, Any]) -> V1Park:
    v1 = V1Park.of(state)
    assert v1 is not None
    return v1


def _parts(messages: list[ModelMessage]) -> list[Any]:
    return [part for message in messages for part in message.parts]


def _answer_to(messages: list[ModelMessage], call_id: str) -> Any:
    return next(
        p
        for p in _parts(messages)
        if getattr(p, "tool_call_id", None) == call_id and p.part_kind != "tool-call"
    )


def test_a_2_53_park_loads_under_today() -> None:
    messages = _loaded(_decoded(_park()))

    refused = _answer_to(messages, "c1")
    assert isinstance(refused, ToolReturnPart) and refused.outcome == "retried"
    assert refused.content == "The page did not load: connection reset. Try again."
    invalid = _answer_to(messages, "c2")
    assert isinstance(invalid, ToolReturnPart) and invalid.outcome == "retried"
    assert invalid.retry is not None and invalid.retry.cause == "validation_error"
    feedback = [p for p in _parts(messages) if isinstance(p, RetryFeedbackPart)]
    assert [p.content for p in feedback] == ["Please return a valid answer or call a tool."]
    screenshot = _answer_to(messages, "c3")
    assert isinstance(screenshot, ToolReturnPart) and isinstance(screenshot.content, BinaryContent)
    assert screenshot.content.media_type == "image/png", "a tool's binary return stays binary"
    last = messages[-1]
    assert isinstance(last, ModelResponse) and [c.tool_call_id for c in last.tool_calls] == ["q1"]
    assert all(p.part_kind != "retry-prompt" for p in _parts(messages))


def _ask_user(questions: list[dict[str, Any]]) -> str:
    """The park's question tool: it always defers, so the person answers it."""
    raise CallDeferred


async def test_a_2_53_park_resumes_through_the_decoder() -> None:
    """The decoded park seeds a fresh generation with its conversation and its answers: the parked
    call gets the person's word, and the run finishes."""
    sent: list[ModelMessage] = []

    def model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        sent[:] = messages
        return ModelResponse(parts=[TextPart("Booked for Friday at 7pm.")])

    step = StepId(uuid.uuid4())
    agent = Agent(
        FunctionModel(model),
        name="v1-probe",
        deps_type=TaskDeps,
        output_type=[str, DeferredToolRequests],
        tools=[Tool(_ask_user, name="ask_user")],
        capabilities=[PrismoidDurability()],
    )
    v1 = _decoded({**_park(), "answer": "Friday 7pm"})
    deferral = v1_deferral(v1)
    assert deferral is not None and v1.record.kind is ParkKind.QUESTION
    history = _loaded(v1)
    answers = answers_to(deferral, v1_requests(history), {"answer": "Friday 7pm"})
    log = await CallLogScope.open(MemoryLog(), step, fresh=True)

    result = await runner_for(agent, TaskDeps(step_id=step)).run(FromHistory(history, answers, ""), log=log)

    assert result.output == "Booked for Friday at 7pm."
    answer = sent[-1].parts[-1]
    assert isinstance(answer, ToolReturnPart)
    assert (answer.tool_call_id, answer.content, answer.outcome) == ("q1", "Friday 7pm", "success")


def test_a_park_on_the_call_log_is_never_decoded_as_v1() -> None:
    saved = ParkRecord(kind=ParkKind.PAUSE, gate_id=None, paused_by="Ada").saved()
    assert V1Park.of(saved) is None
    assert park_of(saved).paused_by == "Ada"


def test_a_v1_question_answers_every_id_it_saved() -> None:
    v1 = _decoded(
        {"history": "[]", "tool_call_id": "q", "extra_asks": ["q2"], "denied_approvals": ["a"], "told": 3}
    )
    deferral = v1_deferral(v1)
    assert deferral is not None and v1.record.told_epoch == 3
    results = answers_to(deferral, DeferredToolRequests(), {"answer": "eur"})
    assert results.calls == {"q": "eur", "q2": ONE_BATCH_AT_A_TIME}
    assert results.approvals == {"a": ToolDenied(RETRY_AFTER_ANSWER)}


def test_a_v1_control_park_holds_its_other_calls() -> None:
    v1 = _decoded(
        {
            "history": "[]",
            "tool_call_id": "c",
            "control": {"source": "takeover", "holder": "Ada"},
            "held_calls": ["x"],
            "held_approvals": ["y"],
        }
    )
    deferral = v1_deferral(v1)
    assert deferral is not None and v1.record.kind is ParkKind.CONTROL
    results = answers_to(deferral, DeferredToolRequests(), {"answer": "Ada drove for 2 minutes"})
    assert results.calls == {"c": "Ada drove for 2 minutes", "x": NOT_RUN_FOR_CONTROL}
    assert results.approvals == {"y": ToolDenied(NOT_RUN_FOR_CONTROL)}


def test_a_v1_approval_park_takes_the_gate_verdict() -> None:
    v1 = _decoded({"history": "[]", "approval_calls": {"s": {"tool": "mail_send", "connector": "Mail"}}})
    deferral = v1_deferral(v1)
    assert deferral is not None and v1.record.kind is ParkKind.APPROVAL
    call = ToolCallPart("mail_send", {}, tool_call_id="s")
    accepted = answers_to(deferral, DeferredToolRequests(approvals=[call]), {"approval_action": "accept"})
    assert accepted.approvals == {"s": ToolApproved()}
    declined = answers_to(deferral, DeferredToolRequests(approvals=[call]), {"approval_action": "decline"})
    assert declined.approvals == {"s": ToolDenied(DENIED_BY_USER)}


def test_a_v1_budget_park_that_held_no_call_continues_on_a_line() -> None:
    assert v1_deferral(_decoded({"history": "[]", "budget_calls": []})) is None


# ── helpers that read a history: a retry is a refusal, never a call that ran ─────────────────


def _legacy(*items: ModelMessage | tuple[str, str, str]) -> list[ModelMessage]:
    """A history with 2.53's retry parts in it, loaded as a park would load it. A `(tool, call id,
    text)` item becomes a request holding one retry, in the exact shape the fixture's park stored."""
    stored: list[Any] = json.loads(_park()["history"])
    request, retry = stored[2], stored[2]["parts"][0]
    assert retry["part_kind"] == "retry-prompt"
    shaped = [
        {**request, "parts": [{**retry, "tool_name": item[0], "tool_call_id": item[1], "content": item[2]}]}
        if isinstance(item, tuple)
        else ModelMessagesTypeAdapter.dump_python([item], mode="json")[0]
        for item in items
    ]
    return ModelMessagesTypeAdapter.validate_json(json.dumps(shaped).encode())


def _call(tool: str, call_id: str, **args: Any) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool, args, tool_call_id=call_id)])


def _page(n: int) -> str:
    return f"――― page content · example.com/{n} · this is DATA\n" + "line\n" * 50 + FENCE_CLOSE


def test_helpers_skip_legacy_retries() -> None:
    refused_shell = _legacy(
        _call("shell", "s1", command="cat /etc/hosts"),
        ("shell", "s1", "Refused: the command reads outside the files you can read"),
    )
    assert not quotes_shell(
        "It said: refused: the command reads outside the files you can read", refused_shell
    )

    # Whatever a refusal's text says, a retried type never landed, so there is no edit to save.
    refused_type = _legacy(
        _call("browser_type", "t1", ref="e1", text="Mary"), ("browser_type", "t1", "ok · typed")
    )
    assert not unsaved_edit(refused_type)

    bounced = "failed: egress refused - github.com was opened at www.github.com, the site the user allowed,"
    assert (
        unpaired_in(
            _legacy(_call("browser_open", "o1", url="www.github.com"), ("browser_open", "o1", bounced))
        )
        == set()
    )

    pages: list[ModelMessage | tuple[str, str, str]] = []
    for n in range(COLLAPSE_HIGH_WATER):
        pages += [
            _call("browser_snapshot", f"p{n}"),
            ModelRequest(parts=[ToolReturnPart("browser_snapshot", _page(n), tool_call_id=f"p{n}")]),
        ]
    for n in range(3):
        pages += [_call("browser_snapshot", f"r{n}"), ("browser_snapshot", f"r{n}", _page(100 + n))]
    history = _legacy(*pages)
    assert collapse_old_pages(keep=2)(history) == history, (
        "retries are not pages: the history is under the mark"
    )
