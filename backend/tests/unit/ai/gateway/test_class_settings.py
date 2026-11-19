# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Class settings reach the wire: each protocol's request carries the output ceiling, and the class's
routing settings (effort, service tier) in that protocol's own field or not at all; only computer use asks
Anthropic to clear old tool results server-side; a bad settings value is dropped, never sent."""

from __future__ import annotations

import json
from typing import Any

import httpx2
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from prismoid.ai.gateway.providers import CONSTRUCTORS, ClassSettings, default_model_settings
from prismoid.schemas.model_class import ModelClass, Protocol

_REPLIES: dict[Protocol, dict[str, Any]] = {
    Protocol.ANTHROPIC: {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 3, "output_tokens": 1},
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
            "input_tokens": 3,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 1,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 4,
        },
    },
    Protocol.GOOGLE: {
        "candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1, "totalTokenCount": 4},
        "modelVersion": "gemini-3.8-flash",
    },
}
_REPLIES[Protocol.OPENAI_COMPATIBLE] = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 0,
    "model": "zai-org/GLM-5.3-Flash",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}
_MODELS = {
    Protocol.ANTHROPIC: "claude-haiku-5-5",
    Protocol.OPENAI: "gpt-6-sol",
    Protocol.GOOGLE: "gemini-3.8-flash",
    Protocol.OPENAI_COMPATIBLE: "zai-org/GLM-5.3-Flash",
}
_URLS = {Protocol.OPENAI_COMPATIBLE: "https://compatible.invalid/v1"}


async def _sent(
    protocol: Protocol, model_class: ModelClass, asked: ClassSettings | None = None
) -> tuple[dict[str, Any], httpx2.Headers]:
    """The body and headers one request of `model_class`, asked with `asked`, sends on `protocol`."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_REPLIES[protocol])

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler), timeout=60)
    model = CONSTRUCTORS[protocol](_MODELS[protocol], _URLS.get(protocol), "test-key", client)
    await model.request(
        [ModelRequest(parts=[UserPromptPart("hi")])],
        default_model_settings(model_class, asked),
        ModelRequestParameters(),
    )
    return json.loads(seen[0].content), seen[0].headers


async def test_by_default_each_protocol_sends_the_ceiling_and_no_effort() -> None:
    anthropic, headers = await _sent(Protocol.ANTHROPIC, ModelClass.CHEAP_CLASSIFIER)
    assert anthropic["max_tokens"] == 16_000
    assert "effort" not in anthropic.get("output_config", {})
    assert "context_management" not in anthropic
    assert "context-management-2025-06-27" not in headers.get("anthropic-beta", "")

    openai, _ = await _sent(Protocol.OPENAI, ModelClass.FRONTIER_REASONER)
    assert openai["max_output_tokens"] == 16_000
    assert "effort" not in openai.get("reasoning", {})

    google, _ = await _sent(Protocol.GOOGLE, ModelClass.FAST_EXECUTOR)
    assert google["generationConfig"]["maxOutputTokens"] == 16_000
    assert "thinking_level" not in google["generationConfig"].get("thinkingConfig", {})


async def test_only_computer_use_asks_the_server_to_clear_old_tool_results() -> None:
    """A maker's skill text and a researcher's pages stay in its context; screenshots are cleared."""
    for model_class in ModelClass:
        if model_class is ModelClass.IMAGE_GEN:
            continue
        body, headers = await _sent(Protocol.ANTHROPIC, model_class)
        clears = model_class is ModelClass.COMPUTER_USE
        assert ("context_management" in body) is clears, model_class
        assert ("context-management-2025-06-27" in headers.get("anthropic-beta", "")) is clears, model_class


async def test_a_class_effort_reaches_each_protocol_in_its_own_field() -> None:
    low = ClassSettings(effort="low")
    anthropic, _ = await _sent(Protocol.ANTHROPIC, ModelClass.CHEAP_CLASSIFIER, low)
    assert anthropic["output_config"]["effort"] == "low"
    openai, _ = await _sent(Protocol.OPENAI, ModelClass.CHEAP_CLASSIFIER, low)
    assert openai["reasoning"]["effort"] == "low"
    google, _ = await _sent(Protocol.GOOGLE, ModelClass.CHEAP_CLASSIFIER, low)
    assert google["generationConfig"]["thinkingConfig"]["thinking_level"] == "LOW"
    compatible, _ = await _sent(Protocol.OPENAI_COMPATIBLE, ModelClass.CHEAP_CLASSIFIER, low)
    assert "reasoning_effort" not in compatible, "a model whose profile takes no effort is sent none"


async def test_the_judge_tier_reaches_the_protocols_that_have_one() -> None:
    flex = ClassSettings(service_tier="flex")
    openai, _ = await _sent(Protocol.OPENAI, ModelClass.JUDGE, flex)
    assert openai["service_tier"] == "flex"
    google, _ = await _sent(Protocol.GOOGLE, ModelClass.JUDGE, flex)
    assert google["serviceTier"] == "flex"
    anthropic, _ = await _sent(Protocol.ANTHROPIC, ModelClass.JUDGE, flex)
    assert "service_tier" not in anthropic
    compatible, _ = await _sent(Protocol.OPENAI_COMPATIBLE, ModelClass.JUDGE, flex)
    assert "service_tier" not in compatible, "a compatible server has no tiers to name"


def test_a_bad_settings_value_is_dropped_and_the_rest_kept() -> None:
    raw = {"effort": "extreme", "service_tier": "flex", "cache_ttl": "1h"}
    assert ClassSettings.read(raw, model_class="judge") == ClassSettings(service_tier="flex")
    assert ClassSettings.read({"effort": 3, "service_tier": ["flex"]}, model_class="judge") == ClassSettings()
    assert ClassSettings.read({"effort": "medium"}, model_class="code") == ClassSettings(effort="medium")
