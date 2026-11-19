# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""One request id, threaded from the edge to the span to the error body.

`request_id` is what turns "it broke" into a query. It is generated (or accepted from
`X-Request-Id`), attached to the request, put on the Logfire span, returned in the header, and
embedded in every error envelope - so the id a user pastes into a support message is the
id that finds the trace.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Final

import logfire
from fastapi import FastAPI, Request, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from prismoid.api.errors import ErrorResponse, envelope
from prismoid.conf import settings
from prismoid.observability import PROBE_PATHS

REQUEST_ID_HEADER = "X-Request-Id"
PROTOCOL_HEADER = "X-Prismoid-Protocol"  # 3 since the Prismoid rename (ADR-099), 2 since ADR-067's
PROTOCOL_VERSION = "3"


async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    rid = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
    request.state.request_id = rid

    with logfire.span(
        "{method} {path}",
        method=request.method,
        path=request.url.path,
        request_id=rid,
    ):
        response = await call_next(request)

    response.headers[REQUEST_ID_HEADER] = rid
    response.headers[PROTOCOL_HEADER] = PROTOCOL_VERSION
    return response


# Every route reads its whole body before anyone is authenticated: FastAPI parses the body before
# the route's dependencies run, and `ActorDep` is one. So a stranger with no account reaches each
# route's cap, and the cap is machine-sized unless the body carries files. The routine fire door
# streams its body under its own smaller cap before any parse (`api/deps.fire_payload`).
#
# Sized for the largest honest small body: a 60,000-character channel line or email, JSON-escaped.
BODY_BYTES: Final = 1024 * 1024
# The routes whose body carries files or a whole document, at `max_body_bytes`: a task's and a
# follow-up's base64 attachments, a skill's bundle, a bot's three authored texts, and an artifact
# saved from the editor.
_CARRIERS: Final = tuple(
    re.compile(pattern)
    for pattern in (
        r"/v1/tasks",
        r"/v1/tasks/[^/]+/follow_up",
        r"/v1/skills(/[^/]+)?",
        r"/v1/bots(/[^/]+)?",
        r"/v1/artifacts/[^/]+/revise",
    )
)


def body_cap(path: str) -> int:
    """The largest body a route reads: `max_body_bytes` for a route whose body carries files,
    `BODY_BYTES` for every other."""
    if any(carrier.fullmatch(path) for carrier in _CARRIERS):
        return settings.api.max_body_bytes
    return BODY_BYTES


class BodySizeLimit:
    """413 for a request body over its route's `body_cap`, on every route, before anything parses it.

    A declared Content-Length over the cap is refused without reading a byte. A chunked or lying
    body is counted as it streams: the moment it passes the cap the 413 goes out, the route reads a
    disconnect, and whatever it answers after is dropped. Without this a JSON route reads and
    decodes any size of body just to refuse one field of it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cap = body_cap(scope["path"])
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > cap:
            await _too_large(scope, send, cap)
            return
        received, refused, started = 0, False, False

        async def counted() -> Message:
            nonlocal received, refused, started
            if refused:  # the route keeps reading: it hears the disconnect, never the rest
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > cap:
                    refused = True
                    if not started:
                        started = True
                        await _too_large(scope, send, cap)
                    return {"type": "http.disconnect"}
            return message

        async def guarded(message: Message) -> None:
            nonlocal started
            if refused:
                return  # the 413 already answered this request
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        await self.app(scope, counted, guarded)


async def _too_large(scope: Scope, send: Send, cap: int) -> None:
    rid = str(scope.get("state", {}).get("request_id", "-"))
    response = ErrorResponse(
        status_code=413,
        content=envelope("payload_too_large", f"request bodies are capped at {cap} bytes", rid),
    )
    await response(scope, _no_body, send)


async def _no_body() -> Message:
    return {"type": "http.disconnect"}


def install(app: FastAPI) -> None:
    app.middleware("http")(request_context)


class QuietProbe:
    """A probe's request runs with instrumentation suppressed, so the load balancer's `/healthz`
    every few seconds writes no span, its SQL included (ADR-101); the ASGI server span outside
    every middleware is skipped by `instrument_fastapi(excluded_urls=PROBE_URLS)`."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] in PROBE_PATHS:
            with logfire.suppress_instrumentation():
                await self.app(scope, receive, send)
            return
        await self.app(scope, receive, send)
