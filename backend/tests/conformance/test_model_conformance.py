# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The model conformance check, once: a row may serve the judge only if one response streams
text and ends on the output tool. Offline on `FunctionModel` always; the live row
(gpt-6-luna) runs only when selected with `-m live` and its provider key is set."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from prismoid.ai.gateway import conformance
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.providers import api_key_for
from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.usage import UsageRecord

ARGS = '{"answer": "11"}'
ROW = ModelSpec(
    provider="fake", model_id="m", family=Family("fake"), protocol="openai", classes=(ModelClass.JUDGE,)
)


def _unused(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("unused")])


async def _conforming(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
    yield "11 is larger; "
    yield "answering now."
    yield {0: DeltaToolCall(name=conformance.OUTPUT_TOOL, json_args=ARGS)}


async def _text_only(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
    yield "11 is larger."


async def _tool_only(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
    yield {0: DeltaToolCall(name=conformance.OUTPUT_TOOL, json_args=ARGS)}


async def _bad_args(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
    yield "11 is larger."
    yield {0: DeltaToolCall(name=conformance.OUTPUT_TOOL, json_args='{"wrong": 1}')}


def _streaming(fn: object) -> FunctionModel:
    return FunctionModel(_unused, stream_function=fn)  # pyright: ignore[reportArgumentType]


async def test_text_then_the_output_tool_in_one_response_conforms() -> None:
    assert (await conformance.check_model(_streaming(_conforming))).ok


@pytest.mark.parametrize(
    ("model", "says"),
    [
        (_streaming(_text_only), "ended on text"),
        (_streaming(_tool_only), "without streaming text"),
        (_streaming(_bad_args), "UsageLimitExceeded"),  # a retry is a second response
        (FunctionModel(_unused), "stream"),  # cannot stream at all
    ],
)
async def test_a_model_that_breaks_the_shape_does_not_conform(model: FunctionModel, says: str) -> None:
    result = await conformance.check_model(model)
    assert not result.ok
    assert says in result.detail


async def _drop(_record: UsageRecord) -> None:
    return None


async def test_the_drill_scores_the_gated_classes_metered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conformance, "build_model", lambda **_: _streaming(_conforming))
    report = await conformance.run_conformance(
        ROW, (ModelClass.JUDGE, ModelClass.FAST_EXECUTOR), emit_usage=_drop
    )
    judge, fast = report.scores
    assert (judge.passed, judge.total, fast.total) == (1, 1, 0)
    assert report.calls == 1 and report.metered


async def test_the_drill_makes_no_call_when_no_class_needs_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conformance, "build_model", lambda **_: pytest.fail("built"))
    report = await conformance.run_conformance(ROW, (ModelClass.CODE,), emit_usage=_drop)
    assert report.calls == 0 and report.scores[0].note == conformance.NOT_GATED


@pytest.mark.live
@pytest.mark.skipif(not api_key_for("openai"), reason="needs the OpenAI key")
async def test_the_live_row_conforms_through_the_real_builder() -> None:
    row = ModelSpec(
        provider="openai",
        model_id="gpt-6-luna",
        family=Family("openai"),
        protocol="openai",
        classes=(ModelClass.JUDGE,),
    )
    report = await conformance.run_conformance(row, (ModelClass.JUDGE,), emit_usage=_drop)
    assert report.scores[0].passed == 1, report.scores[0].note
    assert report.metered
