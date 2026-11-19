# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A credential in a query string never rides a log line or a span (ADR-065): `ticket=` (the
screen socket's `st_` admission, replayable once on another replica inside its window, and the
event streams' single-use `et_` one), `token=` (a long-lived `cs_`/`ck_` credential an old client
may still put there), and the connectors OAuth
callback's `state=` and `code=` are `[redacted]` in uvicorn's
request lines and in the ASGI server span's URL attributes - every other parameter as it was."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterator, MutableMapping
from typing import Any

import httpcore
import httpx
import logfire
import pytest
from logfire.testing import TestExporter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from uvicorn.logging import AccessFormatter

from prismoid.api.app import configure_observability, create_app
from prismoid.observability import (
    QueryCredentialFilter,
    install_log_redaction,
    redact_query_credentials,
    redact_server_span,
)

TICKET = "st_eyJhY3RvciI6IngifQ.c2lnbmF0dXJl"
STREAM_TICKET = "et_" + "Zm9yLXRoZS1ldmVudC1zdHJlYW0tb25seS0wMTIzNDU2Nzg"
TOKEN = "cs_live-session-token-0123456789abcdef"
STATE = "cn_oauth-state-0123456789abcdefghij"
CODE = "authz-code-QWERTY0123456789"


@pytest.mark.parametrize(
    ("text", "want"),
    [
        pytest.param("ticket=st_x&pane=1", "ticket=[redacted]&pane=1", id="a screen query"),
        pytest.param(
            "token=cs_y&since=0&conn=abc", "token=[redacted]&since=0&conn=abc", id="an old stream query"
        ),
        pytest.param(
            f"/v1/tasks/42/events?since=3&ticket={STREAM_TICKET}&conn=tab-1",
            "/v1/tasks/42/events?since=3&ticket=[redacted]&conn=tab-1",
            id="a stream ticket",
        ),
        pytest.param("/v1/tasks/42/stream", "/v1/tasks/42/stream", id="a URL with no query"),
        pytest.param(
            "/v1/tasks/42/events?since=3&follow=false&conn=tab-1",
            "/v1/tasks/42/events?since=3&follow=false&conn=tab-1",
            id="other parameters survive",
        ),
        pytest.param(
            f"ws://api.example/v1/tasks/42/screen?ticket={TICKET}&pane=1",
            "ws://api.example/v1/tasks/42/screen?ticket=[redacted]&pane=1",
            id="a full URL",
        ),
        pytest.param(
            f'127.0.0.1:5 - "WebSocket /v1/tasks/42/stream?token={TOKEN}&follow=false" [accepted]',
            '127.0.0.1:5 - "WebSocket /v1/tasks/42/stream?token=[redacted]&follow=false" [accepted]',
            id="uvicorn's accept line",
        ),
        pytest.param("?tok%65n=cs_z&max_tokens=5", "?tok%65n=[redacted]&max_tokens=5", id="an encoded name"),
        pytest.param("?token=a&token=b", "?token=[redacted]&token=[redacted]", id="a repeated name"),
        pytest.param(
            f"/v1/connectors/oauth/callback?state={STATE}&code={CODE}&iss=x",
            "/v1/connectors/oauth/callback?state=[redacted]&code=[redacted]&iss=x",
            id="the connector OAuth callback",
        ),
    ],
)
def test_the_redaction_helper(text: str, want: str) -> None:
    assert redact_query_credentials(text) == want


class _Keep(logging.Handler):
    def __init__(self, lines: list[str], formatter: logging.Formatter) -> None:
        super().__init__()
        self.lines = lines
        self.setFormatter(formatter)

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture
def captured() -> Iterator[list[str]]:
    """What uvicorn's handlers would write: its two request loggers, after the redaction filter.
    The access line goes through uvicorn's own `AccessFormatter`, which unpacks the argument tuple:
    the filter must keep its shape."""
    install_log_redaction()
    lines: list[str] = []
    access_format = '%(client_addr)s - "%(request_line)s" %(status_code)s'
    handlers = {
        "uvicorn.error": _Keep(lines, logging.Formatter("%(message)s")),
        "uvicorn.access": _Keep(lines, AccessFormatter(access_format, use_colors=False)),
    }
    for name, handler in handlers.items():
        logging.getLogger(name).addHandler(handler)
        logging.getLogger(name).setLevel(logging.INFO)
    try:
        yield lines
    finally:
        for name, handler in handlers.items():
            logging.getLogger(name).removeHandler(handler)


def test_uvicorns_request_lines_never_carry_the_credential(captured: list[str]) -> None:
    install_log_redaction()  # twice: one filter, never a stack of them
    for name in ("uvicorn.error", "uvicorn.access"):
        assert sum(isinstance(f, QueryCredentialFilter) for f in logging.getLogger(name).filters) == 1
    logging.getLogger("uvicorn.error").info(
        '%s - "WebSocket %s" [accepted]', "127.0.0.1:5", f"/v1/tasks/42/screen?ticket={TICKET}&pane=1"
    )
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:5",
        "GET",
        f"/v1/tasks/42/events?token={TOKEN}&since=0",
        "1.1",
        200,
    )
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:5",
        "GET",
        f"/v1/conversations/7/events?since=0&ticket={STREAM_TICKET}",
        "1.1",
        200,
    )
    assert captured == [
        '127.0.0.1:5 - "WebSocket /v1/tasks/42/screen?ticket=[redacted]&pane=1" [accepted]',
        '127.0.0.1:5 - "GET /v1/tasks/42/events?token=[redacted]&since=0 HTTP/1.1" 200 OK',
        '127.0.0.1:5 - "GET /v1/conversations/7/events?since=0&ticket=[redacted] HTTP/1.1" 200 OK',
    ]


def test_the_oauth_callbacks_access_line_carries_neither_state_nor_code(captured: list[str]) -> None:
    """The callback's `state` is its authentication and `code` buys the provider's tokens: the
    access line keeps the path and every other parameter."""
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:5",
        "GET",
        f"/v1/connectors/oauth/callback?state={STATE}&code={CODE}&iss=x",
        "1.1",
        302,
    )
    assert captured == [
        '127.0.0.1:5 - "GET /v1/connectors/oauth/callback?state=[redacted]&code=[redacted]&iss=x HTTP/1.1" 302'
        " Found"
    ]


def test_a_line_that_carries_the_credential_in_its_own_text_is_redacted_too(captured: list[str]) -> None:
    """A no-argument line (an f-string) has `args == ()`, a tuple, and was
    emitted raw. The message is always redacted; a template whose placeholder fills a credential
    is flattened and redacted; uvicorn's argument tuple keeps its shape."""
    log = logging.getLogger("uvicorn.error")
    log.info(f"GET /v1/tasks/42/events?token={TOKEN}&since=0")
    log.info("GET /v1/tasks/42/screen?ticket=%s&pane=%d", TICKET, 1)
    log.info("no credential here: %s=%d", "since", 3)
    assert captured == [
        "GET /v1/tasks/42/events?token=[redacted]&since=0",
        "GET /v1/tasks/42/screen?ticket=[redacted]&pane=1",
        "no credential here: since=3",
    ]
    record = logging.LogRecord("x", logging.INFO, __file__, 1, f"?token={TOKEN}", (), None)
    assert QueryCredentialFilter().filter(record) and record.getMessage() == "?token=[redacted]"


class _Span:
    def __init__(self, attributes: dict[str, Any]) -> None:
        self.attributes = attributes

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


def test_the_span_hook_rewrites_only_the_url_attributes_that_carry_one() -> None:
    span = _Span(
        {
            "http.url": f"http://test/v1/tasks/42/screen?ticket={TICKET}&pane=1",
            "url.query": f"ticket={TICKET}&pane=1",
            "http.target": "/v1/tasks/42/screen",
            "http.route": "/v1/tasks/{task_id}/screen",
        }
    )
    redact_server_span(span, {"query_string": f"ticket={TICKET}&pane=1".encode()})
    assert span.attributes == {
        "http.url": "http://test/v1/tasks/42/screen?ticket=[redacted]&pane=1",
        "url.query": "ticket=[redacted]&pane=1",
        "http.target": "/v1/tasks/42/screen",
        "http.route": "/v1/tasks/{task_id}/screen",
    }


async def _connect(app: Any, path: str, query: str) -> None:
    """One WebSocket connect at the ASGI level, run to the server's close."""
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    inbox.put_nowait({"type": "websocket.connect"})

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "websocket.close":
            inbox.put_nowait({"type": "websocket.disconnect", "code": 1000})

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query.encode(),
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
        "subprotocols": [],
    }
    async with asyncio.timeout(10):
        await app(scope, inbox.get, send)


async def test_the_apps_spans_never_carry_the_credential() -> None:
    """The wiring, on logfire's real pipeline: the app's own `configure_observability` with a test
    exporter appended, so logfire's pending spans (born before any request hook, their message
    built from `http.url`) are exported alongside the finished ones. Both socket routes refuse
    these connects before any database read (a value that is no stream ticket, an unsigned screen
    ticket); the old `token=` rides along to prove it is redacted though no route reads it."""
    exporter = TestExporter()
    configure = logfire.configure

    def capturing(**kwargs: Any) -> Any:
        kwargs["console"] = False
        kwargs["additional_span_processors"] = [
            *kwargs.get("additional_span_processors", ()),
            SimpleSpanProcessor(exporter),
        ]
        return configure(**kwargs)

    task = uuid.uuid4()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(logfire, "configure", capturing)
        app = create_app()
    try:
        await _connect(
            app, f"/v1/tasks/{task}/stream", f"token={TOKEN}&ticket=xx_{STREAM_TICKET}&follow=false"
        )
        await _connect(app, f"/v1/tasks/{task}/screen", f"ticket={TICKET}&pane=1")
    finally:
        configure_observability()  # the process's own pipeline again, for the tests after this one
    spans = [dict(span.attributes or {}) for span in exporter.exported_spans]  # pending ones too
    shown = [
        str(value)
        for attributes in spans
        for key, value in attributes.items()
        if key in ("http.url", "http.target", "url.full", "url.query", "logfire.msg")
    ]
    pending = [attributes for attributes in spans if attributes.get("logfire.span_type") == "pending_span"]
    assert pending, "the pending spans are what a request hook alone would miss"
    assert any("follow=" in text for text in shown) and any("pane=" in text for text in shown), shown
    assert sum("[redacted]" in text for text in shown) >= 4, shown  # pending and finished, both routes
    for fragment in (TOKEN, TOKEN[:9], TOKEN[-9:], TICKET, TICKET[:9], TICKET[-9:], STREAM_TICKET[-9:]):
        assert not [text for text in shown if fragment in text], (fragment, shown)


async def test_the_oauth_callbacks_spans_carry_neither_state_nor_code() -> None:
    """The callback through the app's real pipeline (the operation stubbed: the span is the
    subject, not the exchange): no URL attribute and no span message, pending or finished, carries
    the `cn_` state or the authorization code."""
    from prismoid.operations import connectors as connector_ops

    exporter = TestExporter()
    configure = logfire.configure

    def capturing(**kwargs: Any) -> Any:
        kwargs["console"] = False
        kwargs["additional_span_processors"] = [
            *kwargs.get("additional_span_processors", ()),
            SimpleSpanProcessor(exporter),
        ]
        return configure(**kwargs)

    async def handled(*_: object, **__: object) -> connector_ops.CallbackOutcome:
        return connector_ops.CallbackOutcome(ok=True, message="connected")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(logfire, "configure", capturing)
        patch.setattr(connector_ops, "handle_callback", handled)
        app = create_app()
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get(f"/v1/connectors/oauth/callback?state={STATE}&code={CODE}&iss=x")
            assert r.status_code == 200, r.text
        finally:
            configure_observability()  # the process's own pipeline again, for the tests after this one
    shown = [str(value) for span in exporter.exported_spans for value in (span.attributes or {}).values()]
    assert any("oauth/callback" in text and "iss=x" in text for text in shown), shown
    for fragment in (STATE, STATE[3:12], CODE, CODE[-9:]):
        assert not [text for text in shown if fragment in text], (fragment, shown)


BOT_TOKEN = "123456789:AAH-abcdefghijklmnopqrstuvwxyz0123456"


class _Pool:
    """httpcore's connection pool answering 200 with no network, so the instrumented transport
    stays the real `AsyncHTTPTransport` (the one the OTel httpx instrumentation wraps)."""

    async def handle_async_request(self, request: Any) -> httpcore.Response:
        return httpcore.Response(200, content=b"{}")

    async def __aenter__(self) -> _Pool:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def aclose(self) -> None:
        return None


@pytest.mark.parametrize("process", ["api", "worker"])
async def test_a_telegram_bot_token_never_reaches_an_outbound_span(process: str) -> None:
    """Telegram puts the BotFather token in the URL PATH (`/bot<token>/getUpdates`),
    logfire copies the URL into a pending span as the client span starts, and the old httpx
    `request_hook` never ran for an AsyncClient. The span processor rewrites it as the span
    starts, in both processes, so no attribute and no message carries it."""
    from prismoid.runner.worker import configure_observability as configure_worker

    exporter = TestExporter()
    configure = logfire.configure

    def capturing(**kwargs: Any) -> Any:
        kwargs["console"] = False
        kwargs["additional_span_processors"] = [
            *kwargs.get("additional_span_processors", ()),
            SimpleSpanProcessor(exporter),
        ]
        return configure(**kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(logfire, "configure", capturing)
        (configure_observability if process == "api" else configure_worker)()
    try:
        transport = httpx.AsyncHTTPTransport()
        setattr(transport, "_pool", _Pool())  # noqa: B010 - httpx's private pool, the one seam below the wrap
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates")
    finally:
        configure_observability()  # the process's own pipeline again, for the tests after this one
    shown = [str(value) for span in exporter.exported_spans for value in (span.attributes or {}).values()]
    pending = [
        s for s in exporter.exported_spans if (s.attributes or {}).get("logfire.span_type") == "pending_span"
    ]
    assert pending, "the pending span is what a request hook alone would miss"
    assert any("api.telegram.org/bot[redacted]/getUpdates" in text for text in shown), shown
    for fragment in (BOT_TOKEN, BOT_TOKEN.split(":")[1]):
        assert not [text for text in shown if fragment in text], (fragment, shown)
