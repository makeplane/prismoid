# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Follow-up turns reuse the cached prefix: what a turn sends is stable first and variable last.

Two turns of one thread send the same bytes up to the ask (the static prefix, then the thread so far),
the static breakpoints carry Anthropic's priced 1-hour TTL on the classes a next turn re-reads (a
one-shot class keeps 5 minutes) and no other protocol hears of it, and
OpenAI is sent one routing key per static prefix and model (never per task, never from user text); a
compatible server is sent none.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.agents.base import build, cache_key
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.meter import MeterContext, MeteredModel
from prismoid.ai.gateway.providers import CONSTRUCTORS, default_model_settings
from prismoid.operations.execution.express import express_prompt
from prismoid.schemas.ids import ConversationId, StepId, TaskId
from prismoid.schemas.model_class import Family, ModelClass, Protocol
from prismoid.schemas.thread import ThreadEntry, ThreadTranscript, ThreadTurn
from prismoid.schemas.usage import Unit, UsageRecord

TASK = TaskId(uuid.UUID(int=7))
ROW = ModelSpec(
    provider="fake",
    model_id="m1",
    family=Family("fake"),
    protocol="openai_compatible",
    classes=(ModelClass.FAST_EXECUTOR, ModelClass.FRONTIER_EXECUTOR),
    base_url="https://example.invalid/v1",
    prices={Unit.INPUT_TOKEN: Decimal(0)},
)


_USAGE = {"input_tokens": 3, "output_tokens": 1}
_REPLIES: dict[Protocol, dict[str, Any]] = {
    Protocol.ANTHROPIC: {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": _USAGE,
    },
    Protocol.OPENAI: {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "model": "gpt-6-sol",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "ok", "annotations": []}],
            }
        ],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            **_USAGE,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 4,
        },
    },
    Protocol.GOOGLE: {
        "candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1, "totalTokenCount": 4},
        "modelVersion": "gemini-3.8-flash",
    },
    Protocol.OPENAI_COMPATIBLE: {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "glm",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    },
}
_URLS = {Protocol.OPENAI_COMPATIBLE: "https://compatible.invalid/v1"}


class _Seen(Exception):
    """The request was captured; the run stops there."""


def _turn(n: int, ask: str, reply: str) -> ThreadTurn:
    entries = [
        ThreadEntry(kind="user", text=ask, task_id=TASK, turn=n, seq=2 * n, tokens=9),
        ThreadEntry(kind="assistant", text=reply, task_id=TASK, turn=n, seq=2 * n + 1, tokens=9),
    ]
    return ThreadTurn(task_id=TASK, turn=n, entries=entries)


async def _request(prompt: str) -> str:
    """The Lead's whole first request as text: its instructions, its tools, then the user prompt."""
    seen: list[str] = []

    def capture(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tools = [json.dumps(t.parameters_json_schema, sort_keys=True) for t in info.function_tools]
        last = messages[-1]
        assert isinstance(last, ModelRequest)
        users = [str(p.content) for p in last.parts if isinstance(p, UserPromptPart)]
        seen.append("\n".join([info.instructions or "", *tools, *users]))
        raise _Seen

    runner = build("lead", TaskDeps(task_id=TASK, step_id=StepId(uuid.uuid4())), [ROW])
    with runner.agent.override(model=FunctionModel(capture)), pytest.raises(_Seen):
        await runner.run(prompt)
    return seen[0]


async def test_two_turns_of_a_thread_send_the_same_bytes_up_to_the_ask() -> None:
    first = _turn(1, "Draft the launch note.", "Here is the launch note.")
    second = _turn(2, "Shorter, please.", "Here it is, shorter.")
    ask2, ask3 = "Shorter, please.", "Now add a date."
    turn2 = express_prompt(ask2, ThreadTranscript(turns=[first], current_author="Ada"), ambient="09:00")
    turn3 = express_prompt(
        ask3, ThreadTranscript(turns=[first, second], current_author="Bo"), ambient="09:20"
    )
    request2, request3 = await _request(turn2), await _request(turn3)
    held = request2[: request2.rindex(ask2)]
    assert "Here is the launch note." in held, "the thread so far sits before the ask"
    assert request3.startswith(held), "turn 3 re-sends turn 2's bytes up to its ask"
    assert turn3.endswith("09:20") and turn3.index("# The conversation so far") < turn3.index(ask3)


async def _sent(
    protocol: Protocol, key: str | None, model_class: ModelClass = ModelClass.FAST_EXECUTOR
) -> dict[str, Any]:
    """The body one request with a system instruction and a tool sends on `protocol`."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_REPLIES[protocol])

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler), timeout=60)
    model = CONSTRUCTORS[protocol](
        _REPLIES[protocol].get("model", "gemini-3.8-flash"), _URLS.get(protocol), "test-key", client
    )
    tool = ToolDefinition(name="look", parameters_json_schema={"type": "object", "properties": {}})
    await model.request(
        [ModelRequest(parts=[UserPromptPart("hi")], instructions="You are the Lead.")],
        default_model_settings(model_class, cache_key=key),
        ModelRequestParameters(function_tools=[tool]),
    )
    return json.loads(seen[0].content)


async def test_the_static_prefix_holds_an_hour_only_where_it_is_priced() -> None:
    anthropic = await _sent(Protocol.ANTHROPIC, "prismoid-k")
    assert anthropic["system"][-1]["cache_control"]["ttl"] == "1h"
    assert anthropic["tools"][-1]["cache_control"]["ttl"] == "1h"
    assert anthropic["cache_control"].get("ttl", "5m") == "5m", "the moving breakpoint stays on 5 minutes"
    assert "prompt_cache_key" not in anthropic
    for protocol in (Protocol.OPENAI, Protocol.GOOGLE, Protocol.OPENAI_COMPATIBLE):
        assert '"1h"' not in json.dumps(await _sent(protocol, "prismoid-k")), protocol


@pytest.mark.parametrize("one_shot", [ModelClass.CHEAP_CLASSIFIER, ModelClass.DECIDER, ModelClass.JUDGE])
async def test_a_one_shot_class_never_pays_for_the_hour(one_shot: ModelClass) -> None:
    assert '"1h"' not in json.dumps(await _sent(Protocol.ANTHROPIC, "prismoid-k", one_shot))


async def test_openai_hears_the_cache_key_and_a_compatible_server_does_not() -> None:
    assert (await _sent(Protocol.OPENAI, "prismoid-k"))["prompt_cache_key"] == "prismoid-k"
    assert "prompt_cache_key" not in await _sent(Protocol.OPENAI, None)
    assert "prompt_cache_key" not in await _sent(Protocol.OPENAI_COMPATIBLE, "prismoid-k")


def _key(deps: TaskDeps, name: str = "lead") -> str:
    settings = build(name, deps, [ROW]).agent.model_settings
    key = settings.get("openai_prompt_cache_key") if isinstance(settings, dict) else None
    assert isinstance(key, str)
    return key


def test_two_tasks_on_one_agent_share_the_key_and_no_user_text_reaches_it() -> None:
    first = TaskDeps(task_id=TASK, step_id=StepId(uuid.uuid4()), inputs=("/task/inputs/ada-notes.md",))
    other = TaskDeps(task_id=TaskId(uuid.uuid4()), inputs=("/task/inputs/secret-plan.md",))
    talk = TaskDeps(conversation_id=ConversationId(uuid.uuid4()))
    key = _key(first)
    assert key == _key(other) == _key(talk) == _key(TaskDeps())
    assert key != _key(first, "writer")
    assert len(key) <= 64 and "ada" not in key and str(TASK) not in key


def test_the_key_moves_with_the_prompt_version_and_the_tools() -> None:
    tools = FunctionToolset[Any]()
    tools.add_function(lambda: None, name="look")
    more = FunctionToolset[Any]()
    more.add_function(lambda: None, name="look")
    more.add_function(lambda page: None, name="read")
    key = cache_key("lead", "rules tone", [tools])
    assert key == cache_key("lead", "rules tone", [tools])
    assert key != cache_key("lead", "rules tone2", [tools])
    assert key != cache_key("lead", "rules tone", [more])


async def _routed(model_id: str) -> str:
    """The `prompt_cache_key` one metered OpenAI request on `model_id` sends for the prefix key `prismoid-k`."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_REPLIES[Protocol.OPENAI])

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler), timeout=60)
    spec = ModelSpec("openai", model_id, Family("openai"), "openai", ())
    model = MeteredModel(
        CONSTRUCTORS[Protocol.OPENAI](model_id, None, "k", client), spec, None, MeterContext()
    )
    await model.request(
        [ModelRequest(parts=[UserPromptPart("hi")])],
        default_model_settings(ModelClass.FAST_EXECUTOR, cache_key="prismoid-k"),
        ModelRequestParameters(),
    )
    return json.loads(seen[0].content)["prompt_cache_key"]


async def test_the_key_a_model_hears_carries_that_model() -> None:
    sol = await _routed("gpt-6-sol")
    assert sol == await _routed("gpt-6-sol") and len(sol) <= 64
    assert sol != await _routed("gpt-6-luna")


_HAIKU = {Unit.INPUT_TOKEN: Decimal("1e-7"), Unit.CACHE_WRITE_TOKEN: Decimal("1.25e-7")}
_HOUR_WRITE = {
    **_REPLIES[Protocol.ANTHROPIC],
    "usage": {
        **_USAGE,
        "cache_creation_input_tokens": 1000,
        "cache_creation": {"ephemeral_1h_input_tokens": 800, "ephemeral_5m_input_tokens": 200},
    },
}


async def _billed(prices: dict[Unit, Decimal]) -> tuple[dict[str, Any], UsageRecord]:
    """One metered Anthropic request on a row priced `prices`: the body sent and the ledger row."""
    seen: list[httpx2.Request] = []
    rows: list[UsageRecord] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_HOUR_WRITE)

    async def emit(row: UsageRecord) -> None:
        rows.append(row)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler), timeout=60)
    spec = ModelSpec("anthropic", "claude-haiku-5-5", Family("anthropic"), "anthropic", (), prices=prices)
    model = MeteredModel(
        CONSTRUCTORS[Protocol.ANTHROPIC](spec.model_id, None, "k", client), spec, emit, MeterContext()
    )
    await model.request(
        [ModelRequest(parts=[UserPromptPart("hi")], instructions="You are the Lead.")],
        default_model_settings(ModelClass.FAST_EXECUTOR),
        ModelRequestParameters(),
    )
    return json.loads(seen[0].content), rows[0]


async def test_an_hour_write_is_billed_at_twice_the_input_price() -> None:
    body, row = await _billed({**_HAIKU, Unit.CACHE_WRITE_1H_TOKEN: Decimal("2e-7")})
    assert body["system"][-1]["cache_control"]["ttl"] == "1h"
    assert row.units[Unit.CACHE_WRITE_1H_TOKEN] == 800 and row.units[Unit.CACHE_WRITE_TOKEN] == 200
    hour, five, uncached = (
        Decimal("2e-7") * 800,
        Decimal("1.25e-7") * 200,
        Decimal("1e-7") * row.units[Unit.INPUT_TOKEN],
    )
    assert row.cost_usd == hour + five + uncached


async def test_a_row_that_does_not_price_the_hour_never_asks_for_it() -> None:
    body, _ = await _billed(_HAIKU)
    assert '"1h"' not in json.dumps(body)
    assert body["system"][-1]["cache_control"].get("ttl", "5m") == "5m"
