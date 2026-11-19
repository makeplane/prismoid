# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The web lanes keep content off spans: a search query, a fetched URL's query string and a hosted
image's signed link never reach an exported span, while one span of facts per call still does.

The clients are instrumented exactly as the worker's `logfire.instrument_httpx()` instruments every
transport, so a leak on `url.full` or the span message shows up here as it would in production."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import logfire
import openai
import pytest
from logfire.testing import TestExporter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart

from prismoid.ai.tools.fetch import FetchCaps, fetch_toolset
from prismoid.ai.tools.providers.images import ImageCandidate, wire_for
from prismoid.ai.tools.providers.search import WIRES, SearchCaps, SearchEngine, search_toolset
from tests.support.fetched import on_the_wire, shown

SECRET = "SECRETQUERY"

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def exporter() -> Iterator[TestExporter]:
    """Logfire's real pipeline with a test exporter; the default one again afterwards."""
    exporter = TestExporter()
    logfire.configure(
        send_to_logfire=False, console=False, additional_span_processors=[SimpleSpanProcessor(exporter)]
    )
    try:
        yield exporter
    finally:
        logfire.configure(send_to_logfire=False, console=False)


def instrumented(handler: Handler) -> httpx.AsyncClient:
    """A mock-transport client wearing the same httpx instrumentation the worker installs."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(on_the_wire(handler)))
    logfire.instrument_httpx(client)
    return client


def exported_text(exporter: TestExporter) -> list[str]:
    """Every name, attribute value and event attribute of every exported span, as text."""
    texts: list[str] = []
    for span in exporter.exported_spans:
        texts.append(span.name)
        texts.extend(str(value) for value in (span.attributes or {}).values())
        for event in span.events:
            texts.extend(str(value) for value in (event.attributes or {}).values())
    return texts


def span_facts(exporter: TestExporter, name: str) -> list[dict[str, Any]]:
    return [
        dict(span.attributes or {})
        for span in exporter.exported_spans
        if (span.attributes or {}).get("logfire.span_type") == "span" and span.name.startswith(name)
    ]


async def _noop_journal(*_: Any, **__: Any) -> None:
    return None


async def _noop_bill(_: int) -> None:
    return None


async def _noop_units(_: dict[str, int]) -> None:
    return None


class Ctx:
    def __init__(self, text: str = "") -> None:
        self.tool_call_id = "toolu_spans_01"
        self.messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(text)])] if text else []


SERP = {"web": {"results": [{"title": "T", "url": "https://a.example/", "description": "d"}]}}
ROWS = {"results": [{"title": "T", "url": "https://a.example/", "content": "c", "text": "t", "snippet": "s"}]}
ANTHROPIC = {
    "content": [{"type": "web_search_tool_result", "content": [{"url": "https://a.example/", "title": "T"}]}]
}
OPENAI = {
    "output": [
        {
            "type": "message",
            "content": [
                {
                    "type": "output_text",
                    "text": "answer",
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url": "https://a.example/",
                            "start_index": 0,
                            "end_index": 6,
                        }
                    ],
                }
            ],
        }
    ]
}
BODIES: dict[str, dict[str, Any]] = {"brave": SERP, "anthropic": ANTHROPIC, "openai": OPENAI}


@pytest.mark.parametrize("name", sorted(WIRES))
@pytest.mark.parametrize("status", [200, 429])
async def test_a_search_query_never_reaches_a_span(exporter: TestExporter, name: str, status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=BODIES.get(name, ROWS))

    base = "http://searx.local:8080" if name == "searxng" else ""
    engine = SearchEngine(name=name, api_key="sk-KEY", base_url=base, emit_search=_noop_bill, model="m")
    toolset = search_toolset(
        ("web.search",),
        _noop_journal,
        (engine,),
        SearchCaps(timeout_s=5.0, results=3, max_per_step=3),
        client_factory=lambda: instrumented(handler),
    )
    assert toolset is not None
    search: Any = toolset.tools["web_search"].function
    result = await search(Ctx(), [f"acme merger {SECRET} salaries"])

    assert ("search results" in result) == (status == 200)
    assert not [text for text in exported_text(exporter) if SECRET in text]
    [facts] = span_facts(exporter, "web.search")
    assert facts["engine"] == name


@pytest.mark.parametrize("status", [200, 404])
async def test_a_fetched_query_string_never_reaches_a_span(exporter: TestExporter, status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"content-type": "text/plain"}, content=b"page words")

    async def dns(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    caps = FetchCaps(
        timeout_s=5.0,
        max_bytes=1_000_000,
        max_redirects=3,
        max_per_step=5,
        page_chars=12_000,
        convert_timeout_s=60.0,
        context_anchor=True,
        allow_insecure_localhost=False,
    )
    toolset = fetch_toolset(
        ("web.fetch",), _noop_journal, client_factory=lambda: instrumented(handler), resolver=dns, caps=caps
    )
    assert toolset is not None
    url = f"https://example.com/report?q={SECRET}"
    fetch: Any = shown(toolset.tools["web_fetch"].function)
    result = await fetch(Ctx(f"read {url}"), url)

    assert ("page words" in result) == (status == 200)
    assert not [text for text in exported_text(exporter) if SECRET in text]
    [facts] = span_facts(exporter, "web.fetch")
    assert (facts["host"], facts["path"]) == ("example.com", "/report")


class _HostedImages:
    """An OpenAI-wire server that answers with a signed hosted link instead of inline bytes."""

    def __init__(self, **_: Any) -> None:
        self.images = self

    async def generate(self, **_: Any) -> Any:
        link = f"https://imgen.x.ai/out/1.png?key={SECRET}"
        return SimpleNamespace(data=[SimpleNamespace(b64_json=None, url=link)])


async def test_a_hosted_image_link_never_reaches_a_span(
    exporter: TestExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Hosting(httpx.AsyncClient):
        """The download's client: a mock image host, instrumented as the worker instruments one."""

        def __init__(self, **_: Any) -> None:
            super().__init__(
                transport=httpx.MockTransport(on_the_wire(lambda _: httpx.Response(200, content=b"\x89PNG")))
            )
            logfire.instrument_httpx(self)

    monkeypatch.setattr(openai, "AsyncOpenAI", _HostedImages)
    monkeypatch.setattr(httpx, "AsyncClient", Hosting)
    wire = wire_for("openai_images")
    assert wire is not None
    candidate = ImageCandidate("xai", "grok-imagine", "openai_images", None, "sk-KEY", _noop_units)
    images = await wire(candidate, "a chart", "square", 1)

    assert images == [b"\x89PNG"]
    assert not [text for text in exported_text(exporter) if SECRET in text]
    [facts] = span_facts(exporter, "image.fetch")
    assert (facts["host"], facts["path"]) == ("imgen.x.ai", "/out/1.png")
