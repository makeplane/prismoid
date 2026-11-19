# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What the worker's agent spans carry: what ran and what it cost, never the prompt, completion, tool
argument, tool result or binary content (a step's prompt can hold its creator's private memory, and a
trace backend is a second copy with weaker access control)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from typing import Any

import logfire
import pytest
from logfire.testing import TestExporter
from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
from opentelemetry.instrumentation.httpx import HTTPX2ClientInstrumentor, HTTPXClientInstrumentor
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from pydantic_ai import Agent, BinaryContent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from prismoid.runner import worker

INSTRUCTIONS = "instruction-marker: the creator's private memory says they bank at Northwind"
PROMPT = "prompt-marker: compare the quarterly invoices for Contoso"
TOOL_ARGUMENT = "argument-marker-4417"
TOOL_RESULT = "result-marker-9082"
COMPLETION = "completion-marker: Contoso owes 1,204.55"
PIXELS = b"\x89PNG-binary-marker-6631" * 4
CONTENT = (INSTRUCTIONS, PROMPT, TOOL_ARGUMENT, TOOL_RESULT, COMPLETION)


@pytest.fixture
def booted(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], str]]:
    """The worker's own boot instrumentation with a test exporter appended: `booted()` runs one agent
    that carries every kind of content and returns the text of every span attribute."""
    exporter = TestExporter()
    configure = logfire.configure

    def capturing(**kwargs: Any) -> Any:
        kwargs["console"] = False
        kwargs["additional_span_processors"] = [
            *kwargs.get("additional_span_processors", ()),
            SimpleSpanProcessor(exporter),
        ]
        return configure(**kwargs)

    def run() -> str:
        asyncio.run(_agent().run([PROMPT, BinaryContent(PIXELS, media_type="image/png")]))
        attributes = [dict(span.attributes or {}) for span in exporter.exported_spans]
        return "\n".join(f"{key}={value}" for each in attributes for key, value in each.items())

    process_wide = [
        AsyncPGInstrumentor(),
        HTTPXClientInstrumentor(),
        HTTPX2ClientInstrumentor(),
        SystemMetricsInstrumentor(),
    ]
    already = [each.is_instrumented_by_opentelemetry for each in process_wide]
    with monkeypatch.context() as patch:
        patch.setattr(logfire, "configure", capturing)
        worker.configure_observability()
    try:
        yield run
    finally:
        Agent.instrument_all(False)
        for each, was in zip(process_wide, already, strict=True):
            if not was and each.is_instrumented_by_opentelemetry:
                each.uninstrument()
        configure(send_to_logfire=False, console=False)


def lookup(query: str) -> str:
    return TOOL_RESULT


def _agent() -> Agent[None, str]:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("lookup", {"query": TOOL_ARGUMENT}, tool_call_id="c1")])
        return ModelResponse(parts=[TextPart(COMPLETION)])

    return Agent(FunctionModel(model), instructions=INSTRUCTIONS, tools=[lookup])


def test_agent_content_is_not_exported(booted: Callable[[], str]) -> None:
    """Spans carry the model, tools and token counts of a run, and none of its content."""
    shown = booted()
    assert "gen_ai.usage.input_tokens" in shown and "lookup" in shown, "the agent loop is still traced"
    assert "binary-marker" not in shown and "iVBOR" not in shown, "an image never rides a span"
    for marker in CONTENT:
        assert marker not in shown, marker
