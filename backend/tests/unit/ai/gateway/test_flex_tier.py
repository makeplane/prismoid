# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The flex tier is priced as data: a call the provider served on flex bills the row's flex rates,
the tier the response reports wins over the one asked for, and a row without flex prices is never
sent flex (so it is never billed a discount it did not get, nor a rate it does not price)."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx2
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.meter import MeterContext, MeteredModel
from prismoid.ai.gateway.providers import CONSTRUCTORS, ClassSettings, default_model_settings
from prismoid.schemas.model_class import Family, ModelClass, Protocol
from prismoid.schemas.usage import Unit, UsageRecord

_STANDARD = {
    Unit.INPUT_TOKEN: Decimal("1e-7"),
    Unit.OUTPUT_TOKEN: Decimal("5e-7"),
    Unit.CACHE_READ_TOKEN: Decimal("1e-8"),
}
_FLEX = {
    Unit.FLEX_INPUT_TOKEN: Decimal("5e-8"),
    Unit.FLEX_OUTPUT_TOKEN: Decimal("2.5e-7"),
    Unit.FLEX_CACHE_READ_TOKEN: Decimal("5e-9"),
}


def _reply(tier: str) -> dict[str, Any]:
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "model": "gpt-6-luna",
        "status": "completed",
        "service_tier": tier,
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
            "input_tokens": 1000,
            "output_tokens": 100,
            "total_tokens": 1100,
            "input_tokens_details": {"cached_tokens": 400},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


async def _judged(prices: dict[Unit, Decimal], *, served: str) -> tuple[dict[str, Any], UsageRecord]:
    """One metered OpenAI judge request (class settings ask flex) on a row priced `prices`, answered
    as served on `served`: the body sent and the ledger row."""
    seen: list[httpx2.Request] = []
    rows: list[UsageRecord] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_reply(served))

    async def emit(row: UsageRecord) -> None:
        rows.append(row)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler), timeout=60)
    spec = ModelSpec("openai", "gpt-6-luna", Family("openai"), "openai", (), prices=prices)
    model = MeteredModel(
        CONSTRUCTORS[Protocol.OPENAI](spec.model_id, None, "k", client), spec, emit, MeterContext()
    )
    await model.request(
        [ModelRequest(parts=[UserPromptPart("grade this")])],
        default_model_settings(ModelClass.JUDGE, ClassSettings(service_tier="flex")),
        ModelRequestParameters(),
    )
    return json.loads(seen[0].content), rows[0]


async def test_a_flex_response_is_billed_at_the_flex_price() -> None:
    body, row = await _judged({**_STANDARD, **_FLEX}, served="flex")
    assert body["service_tier"] == "flex"
    assert row.units == {
        Unit.FLEX_INPUT_TOKEN: 600,
        Unit.FLEX_OUTPUT_TOKEN: 100,
        Unit.FLEX_CACHE_READ_TOKEN: 400,
        Unit.REQUEST: 1,
    }
    assert row.cost_usd == Decimal("5e-8") * 600 + Decimal("2.5e-7") * 100 + Decimal("5e-9") * 400


async def test_the_tier_the_response_reports_wins_over_the_one_asked_for() -> None:
    _, row = await _judged({**_STANDARD, **_FLEX}, served="default")
    assert row.cost_usd == Decimal("1e-7") * 600 + Decimal("5e-7") * 100 + Decimal("1e-8") * 400


async def test_a_row_without_a_flex_price_is_never_sent_flex() -> None:
    body, row = await _judged(_STANDARD, served="default")
    assert "service_tier" not in body
    assert Unit.INPUT_TOKEN in row.units and Unit.FLEX_INPUT_TOKEN not in row.units

    partly = {**_STANDARD, Unit.FLEX_INPUT_TOKEN: Decimal("5e-8"), Unit.FLEX_OUTPUT_TOKEN: Decimal("2.5e-7")}
    body, _ = await _judged(partly, served="default")
    assert "service_tier" not in body, "a flex card missing a unit the row prices is no flex card"
