# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The gateway says where a model request's time went. Live, a writer step sat 707 s between two
model calls with nothing in the worker log: a request that idles out, fails over to the next row
and idles out again is invisible, and so is a wait for the provider's concurrency slot. Now:

* every failed model request - each fallback hop - logs its row, why it failed (idle timeout,
  rate limit, provider error, refused, content filter, connection) and how long it took;
* a request that answered only after a long time says so;
* a wait for a provider slot longer than a few seconds says so."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ContentFilterError, ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import AgentInfo, DeltaToolCalls, FunctionModel

from prismoid.ai.gateway import meter, throttle
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.meter import MeteredModel, failure_reason
from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.usage import Unit


class Said:
    """A stand-in for `logfire`: what the gateway said, by level."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str, dict[str, Any]]] = []

    def warn(self, template: str, **attrs: Any) -> None:
        self.lines.append(("warn", template, attrs))

    def info(self, template: str, **attrs: Any) -> None:
        self.lines.append(("info", template, attrs))


@pytest.fixture
def said(monkeypatch: pytest.MonkeyPatch) -> Said:
    fake = Said()
    monkeypatch.setattr(meter, "logfire", fake)
    monkeypatch.setattr(throttle, "logfire", fake)
    return fake


def _spec(model_id: str) -> ModelSpec:
    return ModelSpec(
        provider="openai",
        model_id=model_id,
        family=Family("openai"),
        protocol="openai",
        classes=(ModelClass.FAST_EXECUTOR,),
        prices={Unit.INPUT_TOKEN: Decimal("0")},
    )


def _timeout() -> ModelAPIError:
    try:
        try:
            raise httpx2.ReadTimeout("timed out")
        except httpx2.ReadTimeout as cause:
            raise ModelAPIError("m", "Request timed out.") from cause
    except ModelAPIError as exc:
        return exc


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (_timeout(), "idle_timeout"),
        (httpx2.ReadTimeout("raw, from a stream"), "idle_timeout"),
        (httpx2.ConnectTimeout("no route"), "timeout"),
        (ModelHTTPError(429, "m"), "rate_limited"),
        (ModelHTTPError(503, "m"), "provider_error"),
        (ModelHTTPError(400, "m"), "refused"),
        (ModelHTTPError(413, "m"), "refused"),
        (ModelHTTPError(401, "m"), "http_401"),
        (ContentFilterError("refusal"), "content_filter"),
        (ModelAPIError("m", "Connection error."), "connection"),
        (httpx2.RemoteProtocolError("peer closed"), "connection"),
    ],
)
def test_a_failure_is_named_by_its_reason(exc: BaseException, reason: str) -> None:
    assert failure_reason(exc) == reason


def _failing(exc: Exception) -> FunctionModel:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise exc

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        raise exc
        yield ""  # pragma: no cover - an async generator

    return FunctionModel(model, stream_function=stream)


def _answering() -> FunctionModel:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("ok")])

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        yield "ok"

    return FunctionModel(model, stream_function=stream)


async def _noop(ctx: Any, events: Any) -> None:
    async for _ in events:
        pass


@pytest.mark.parametrize("streamed", [False, True])
async def test_each_fallback_hop_says_which_row_failed_why_and_after_how_long(
    said: Said, streamed: bool
) -> None:
    chain = FallbackModel(
        MeteredModel(_failing(_timeout()), _spec("first"), None, meter.MeterContext()),
        MeteredModel(_answering(), _spec("second"), None, meter.MeterContext()),
    )
    agent = Agent(chain, output_type=str)
    result = await agent.run("go", event_stream_handler=_noop if streamed else None)
    assert result.output == "ok"
    (line,) = [attrs for level, _, attrs in said.lines if level == "warn"]
    assert line["ref"] == "openai:first" and line["reason"] == "idle_timeout"
    assert isinstance(line["elapsed_s"], float) and line["elapsed_s"] >= 0


async def test_a_raw_stream_error_is_said_too(said: Said) -> None:
    metered = MeteredModel(
        _failing(httpx2.RemoteProtocolError("peer closed")), _spec("only"), None, meter.MeterContext()
    )
    with pytest.raises(httpx2.RemoteProtocolError):
        await Agent(metered, output_type=str).run("go", event_stream_handler=_noop)
    assert [(a["ref"], a["reason"]) for lvl, _, a in said.lines if lvl == "warn"] == [
        ("openai:only", "connection")
    ]


async def test_a_quick_answer_says_nothing_and_a_slow_one_says_how_long(
    said: Said, monkeypatch: pytest.MonkeyPatch
) -> None:
    metered = MeteredModel(_answering(), _spec("only"), None, meter.MeterContext())
    await Agent(metered, output_type=str).run("go")
    assert said.lines == []
    monkeypatch.setattr(meter, "SLOW_REQUEST_S", 0.0)
    await Agent(metered, output_type=str).run("go")
    ((level, _, attrs),) = said.lines
    assert level == "info" and attrs["ref"] == "openai:only"


async def test_a_long_wait_for_a_provider_slot_is_said(said: Said, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(throttle, "SLOT_WAIT_LOG_S", 0.05)
    throttle.reset()
    limiter = throttle.limiter_for("waity")
    await limiter.acquire("model:a")  # the one slot (dev conf: one replica, at least one slot)
    for _ in range(limiter.max_running - 1):
        await limiter.acquire("model:a")

    async def free_soon() -> None:
        await asyncio.sleep(0.1)
        limiter.release()

    freeing = asyncio.create_task(free_soon())
    await limiter.acquire("model:b")
    await freeing
    ((level, _, attrs),) = said.lines
    assert level == "warn" and attrs["provider"] == "waity" and attrs["waited_s"] >= 0.05
    limiter.release()
    said.lines.clear()
    await limiter.acquire("model:c")  # a free slot: no wait, nothing said
    assert said.lines == []
    throttle.reset()


def test_a_failed_request_logs_its_kind_never_the_providers_words() -> None:
    """A provider's error body can echo the key it was sent ("Incorrect API key provided: ...");
    the failure line carries the error's type and status, never its text."""
    echoed = "Incorrect API key provided: <redacted-test-key>"
    exc = ModelHTTPError(401, "m", body={"error": echoed})
    said = meter.failed_error(exc)
    assert "redacted-test-key" not in said and "401" in said and "ModelHTTPError" in said
