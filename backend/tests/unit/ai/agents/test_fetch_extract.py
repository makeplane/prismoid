# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`web_fetch`'s views (ADR-046): `extract` returns what serves the goal, and code drops every quote the
page does not hold before the agent reads it; `table` returns the page's tables; `raw`, the default,
shows the page as read, and so does `extract` with no goal or no reader. The page reader itself is a built agent, so the meter rides
its calls. No network: httpx.MockTransport plays the site and a fake reader plays the cheap model."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any, ClassVar

import httpx
import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from prismoid.ai.agents import mounts
from prismoid.ai.agents import page_reader as page_reader
from prismoid.ai.agents.base import build
from prismoid.ai.capabilities import durability
from prismoid.ai.capabilities.durability import LOG_VERSION, CallLogScope
from prismoid.ai.deps import CallEntry, Generation, RunKey, TaskDeps
from prismoid.ai.gateway import resolve
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.tools.fetch import FetchCaps, fetch_toolset
from prismoid.ai.tools.reading import (
    READ_CHUNK,
    READ_CHUNKS,
    PageReading,
    read_for,
    reading_prompt,
    render_reading,
)
from prismoid.schemas.ids import StepId
from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.usage import Unit
from tests.support.fetched import on_the_wire, shown

PAGE = "The bridge opened in 1932. Its main span is 503 metres long.\n\n| year | riders |\n| --- | --- |\n| 2024 | 9k |"
URL = "https://example.com/bridge"


class Ctx:
    tool_call_id = "toolu_web_01"
    messages: ClassVar[list[ModelMessage]] = [ModelRequest(parts=[UserPromptPart(f"read {URL}")])]


def _fetch(reader: Any) -> Any:
    def site(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/plain"})

    async def dns(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    dials = FetchCaps(5.0, 1_000_000, 3, 5, 12_000, 60.0, True, False)
    toolset = fetch_toolset(
        ("web.fetch",),
        None,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(on_the_wire(site))),
        resolver=dns,
        caps=dials,
        reader=reader,
    )
    assert toolset is not None
    return shown(toolset.tools["web_fetch"].function)


async def test_an_extract_quote_not_in_the_page_is_dropped() -> None:
    goals: list[str] = []

    async def reader(goal: str, text: str) -> PageReading:
        goals.append(goal)
        return PageReading(
            found=True,
            quotes=["Its main span is 503 metres long.", "The bridge cost $4 billion."],
            summary="Opened 1932; 503 m span.",
        )

    result = await _fetch(reader)(Ctx(), URL, goal="how long is the main span", view="extract")

    assert goals == ["how long is the main span"]
    assert '"Its main span is 503 metres long."' in result
    assert "4 billion" not in result, "a quote the page does not hold never reaches the agent"
    assert "1 quote(s) the reader gave are not in the page and were dropped" in result
    assert "The reader's summary (a model's words, not the page's)" in result


async def test_an_extract_reads_a_few_chunks_a_few_at_a_time_and_says_where_it_stopped() -> None:
    """A huge page costs a few reader calls, never one per chunk, and the result names the rest."""
    calls: list[int] = []
    flying = [0, 0]  # now, most at once

    async def reader(goal: str, text: str) -> PageReading:
        flying[0] += 1
        flying[1] = max(flying)
        await asyncio.sleep(0)
        flying[0] -= 1
        calls.append(len(text))
        return PageReading(found=False)

    page = "word " * (READ_CHUNK * 10)
    shown = render_reading(await read_for(reader, "the price", page), len(page))

    assert calls == [READ_CHUNK] * READ_CHUNKS, "only the first chunks are read"
    assert flying[1] < READ_CHUNKS, "the reader calls are bounded in flight"
    read = READ_CHUNK * READ_CHUNKS
    assert f"Read the first {read} of {len(page)} characters" in shown
    assert f'view="raw", start_index={read})' in shown


async def test_a_quote_with_other_spacing_or_curly_marks_is_still_the_page() -> None:
    async def reader(goal: str, text: str) -> PageReading:
        return PageReading(found=True, quotes=["The  bridge\nopened in 1932."])

    result = await _fetch(reader)(Ctx(), URL, goal="when did it open", view="extract")
    assert '"The bridge opened in 1932."' in result and "dropped" not in result


async def test_table_shows_only_the_tables_and_the_default_shows_the_page() -> None:
    async def reader(goal: str, text: str) -> PageReading:
        raise AssertionError("raw never calls the reader")

    fetch = _fetch(reader)
    table = await fetch(Ctx(), URL, view="table")
    assert "| 2024 | 9k |" in table and "opened in 1932" not in table
    raw = await fetch(Ctx(), URL, goal="span")
    assert "opened in 1932" in raw and "| 2024 | 9k |" in raw, "raw is the default view"


async def test_a_reader_that_fails_shows_the_page_as_read() -> None:
    async def reader(goal: str, text: str) -> PageReading:
        raise RuntimeError("model down")

    result = await _fetch(reader)(Ctx(), URL, goal="span", view="extract")
    assert "the reader could not run (RuntimeError)" in result and "503 metres" in result


async def test_the_page_reader_builds_and_answers_a_page_reading() -> None:
    """Built through `build()`, the one constructor, so the meter rides every reader call."""

    def model(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        [output] = info.output_tools
        args = {"found": True, "quotes": ["Its main span is 503 metres long."], "summary": "503 m."}
        return ModelResponse(parts=[ToolCallPart(output.name, args)])

    row = ModelSpec(
        provider="fake",
        model_id="m-cheap",
        family=Family("fake"),
        protocol="openai_compatible",
        classes=(ModelClass.CHEAP_CLASSIFIER,),
        base_url="https://example.invalid/v1",
        prices={Unit.INPUT_TOKEN: Decimal("0.000001")},
    )
    runner = build(str(page_reader.PAGE_READER.name), TaskDeps(), [row])
    with runner.agent.override(model=FunctionModel(model)):
        result = await runner.run(reading_prompt("span", PAGE))

    assert result.output == PageReading(
        found=True, quotes=["Its main span is 503 metres long."], summary="503 m."
    )


class _Log:
    """A call log that keeps what it is handed."""

    def __init__(self) -> None:
        self.rows: list[CallEntry] = []

    async def latest(self, step: StepId) -> Generation:
        return Generation(None, {})

    async def record(self, key: RunKey, entry: CallEntry) -> None:
        self.rows.append(entry)


async def test_the_mounted_reader_is_metered_and_off_the_steps_call_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The web door's reader is the page reader built by `build()`: every call emits a usage record,
    and nothing it runs lands in the step's call log (it would skew the step's replay)."""

    def model(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        [output] = info.output_tools
        return ModelResponse(parts=[ToolCallPart(output.name, {"found": True, "quotes": [], "summary": "s"})])

    seen: dict[str, Any] = {}
    monkeypatch.setattr(mounts, "fetch_toolset", lambda *_, **kw: seen.update(kw))
    monkeypatch.setattr(resolve, "build_model", lambda **_: FunctionModel(model))
    usage: list[object] = []

    async def emit(record: object) -> None:
        usage.append(record)

    row = ModelSpec(
        provider="fake",
        model_id="m-any",
        family=Family("fake"),
        protocol="openai_compatible",
        classes=tuple(ModelClass),
        base_url="https://example.invalid/v1",
        prices={Unit.INPUT_TOKEN: Decimal("0.000001")},
    )
    build("researcher", TaskDeps(emit_usage=emit), [row])
    reader = seen["reader"]
    log = _Log()
    scope = CallLogScope(log, RunKey(StepId(uuid.uuid4()), 0, LOG_VERSION), {})

    with durability.active(scope):
        reading = await reader("span", PAGE)

    assert reading == PageReading(found=True, quotes=[], summary="s")
    assert len(usage) == 1, "the reader's model call is metered"
    assert log.rows == [] and scope.requests == 0, "the reader runs off the step's call log"
