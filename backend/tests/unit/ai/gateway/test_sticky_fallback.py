# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""One retry policy (ADR-024): the chain fails over only on the provider's weather or a prompt too long
for one row's window, and a run that failed over stays on the row that answered instead of re-asking
the browned-out head every request.

A brown-out fake: row A answers every request with a 529 or a 503, row B answers. The run calls a
tool twice, so it makes three model requests; each case counts what reached each row."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelContextWindowExceededError,
    ModelHTTPError,
    ModelRequestRejectedError,
)
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from prismoid.ai.gateway import resolve, throttle
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.schemas.model_class import Family, ModelClass


class Row:
    """One catalog row's fake provider: it raises `fails` (when set) or runs a one-tool loop."""

    def __init__(self, fails: Callable[[], Exception] | None = None) -> None:
        self.fails = fails
        self.calls = 0

    def _answer(self, messages: list[ModelMessage]) -> ModelResponse:
        self.calls += 1
        if self.fails is not None:
            raise self.fails()
        returned = [p for m in messages for p in getattr(m, "parts", ()) if isinstance(p, ToolReturnPart)]
        if len(returned) < 2:
            return ModelResponse(parts=[ToolCallPart("look", {})])
        return ModelResponse(parts=[TextPart("done")])

    def model(self, name: str) -> FunctionModel:
        def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return self._answer(messages)

        async def stream(
            messages: list[ModelMessage], info: AgentInfo
        ) -> AsyncIterator[str | DeltaToolCalls]:
            part = self._answer(messages).parts[0]
            if isinstance(part, ToolCallPart):
                yield {0: DeltaToolCall(name=part.tool_name, json_args="{}")}
            else:
                assert isinstance(part, TextPart)
                yield part.content

        return FunctionModel(answer, stream_function=stream, model_name=name)


def _spec(model_id: str) -> ModelSpec:
    return ModelSpec(
        provider="local",
        model_id=model_id,
        family=Family("local"),
        protocol="openai_compatible",
        classes=(ModelClass.FAST_EXECUTOR,),
        base_url="http://localhost:11434/v1",
    )


def _chain(monkeypatch: pytest.MonkeyPatch, rows: dict[str, Row]) -> Model:
    """The real `resolve()` over the rows, in order, each built as its fake."""

    def build(*, provider: str, model_id: str, protocol: str, base_url: str | None) -> Model:
        return rows[model_id].model(model_id)

    monkeypatch.setattr(resolve, "build_model", build)
    throttle.reset()
    order = {ModelClass.FAST_EXECUTOR.value: [_spec(name).ref for name in rows]}
    return resolve.resolve(ModelClass.FAST_EXECUTOR, [_spec(name) for name in rows], order=order)


async def _run(model: Model, *, streamed: bool) -> str:
    def look() -> str:
        return "seen"

    agent = Agent(model, output_type=str, tools=[look])

    async def drain(ctx: Any, events: Any) -> None:
        async for _ in events:
            pass

    result = await agent.run("go", event_stream_handler=drain if streamed else None)
    return result.output


@pytest.mark.parametrize("status", [529, 503])
@pytest.mark.parametrize("streamed", [False, True])
async def test_after_one_failover_every_later_request_goes_to_the_fallback(
    monkeypatch: pytest.MonkeyPatch, status: int, streamed: bool
) -> None:
    a, b = Row(lambda: ModelHTTPError(status, "a")), Row()
    assert await _run(_chain(monkeypatch, {"a": a, "b": b}), streamed=streamed) == "done"
    assert (a.calls, b.calls) == (1, 3)


@pytest.mark.parametrize("streamed", [False, True])
async def test_a_refused_request_never_fails_over(monkeypatch: pytest.MonkeyPatch, streamed: bool) -> None:
    a, b = Row(lambda: ModelHTTPError(400, "a")), Row()
    with pytest.raises(ModelHTTPError) as raised:
        await _run(_chain(monkeypatch, {"a": a, "b": b}), streamed=streamed)
    assert raised.value.status_code == 400
    assert (a.calls, b.calls) == (1, 0)


def _too_long() -> Exception:
    """A prompt past the row's context window, as each provider's adapter classifies it (a 400)."""
    return ModelHTTPError.for_category(ModelContextWindowExceededError, status_code=400, model_name="a")


@pytest.mark.parametrize("streamed", [False, True])
async def test_a_prompt_too_long_for_one_row_moves_to_the_next(
    monkeypatch: pytest.MonkeyPatch, streamed: bool
) -> None:
    a, b = Row(_too_long), Row()
    assert await _run(_chain(monkeypatch, {"a": a, "b": b}), streamed=streamed) == "done"
    assert (a.calls, b.calls) == (1, 3)


@pytest.mark.parametrize(
    ("error", "moves"),
    [
        (lambda: ModelHTTPError(429, "a"), True),
        (lambda: ModelHTTPError(401, "a"), True),
        (lambda: ModelAPIError("a", "Connection error."), True),
        (lambda: ModelHTTPError(413, "a"), False),
        (lambda: ModelHTTPError(422, "a"), False),
        (_too_long, True),
        (
            lambda: ModelHTTPError.for_category(ModelRequestRejectedError, status_code=400, model_name="a"),
            False,
        ),
    ],
)
def test_the_chain_moves_on_the_providers_weather_a_refused_key_and_a_prompt_too_long_only(
    error: Callable[[], Exception], moves: bool
) -> None:
    assert throttle.fails_over(error()) is moves


async def test_a_second_brown_out_moves_on_and_comes_back_around(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback browning out too moves the run to the next row; the first row is tried last."""
    a, b, c = Row(lambda: ModelHTTPError(529, "a")), Row(), Row()
    model = _chain(monkeypatch, {"a": a, "b": b, "c": c})
    await _run(model, streamed=False)
    b.fails = lambda: ModelHTTPError(503, "b")
    await _run(model, streamed=False)
    assert (a.calls, b.calls, c.calls) == (1, 4, 3)
    assert isinstance(model, resolve.StickyFallback)
    assert [m.model_name for m in model.models] == ["c", "a", "b"]
