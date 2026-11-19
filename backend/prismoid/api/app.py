# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The ASGI app. Assembly only - every decision it makes lives somewhere else.

Read it top to bottom and you have the whole request path: warm the pool, instrument, allow the
origins we said we would, install the one error envelope, mount the one route table.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import logfire
from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from prismoid import stdout_log
from prismoid.api import errors, middleware, spa, urls
from prismoid.api.hub import hub
from prismoid.api.middleware import PROTOCOL_HEADER, PROTOCOL_VERSION, REQUEST_ID_HEADER
from prismoid.conf import settings
from prismoid.db.engine import dispose_engine, set_application_name, warm_pool
from prismoid.observability import (
    PROBE_URLS,
    SCRUB_PATTERNS,
    UrlCredentialSpanProcessor,
    install_log_redaction,
    redact_server_span,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # Name our connections before the pool opens - `prismoid-api` in pg_stat_activity (a diagnostic
    # that pays for itself the first time a connection misbehaves). Must precede warm_pool.
    set_application_name("prismoid-api")

    # A warmed pool. The first request should not be the one that pays for the handshake.
    await warm_pool()

    # The worker owns all machine work.
    #
    # The API gets a `ClientDispatcher` - a `DBOSClient`, which can **enqueue and nothing else**.
    # It deliberately does NOT call `DBOS.launch()`: launching starts the queue *consumers*, and an
    # API that consumes its own queue is an API that spawns Docker sandboxes and calls language
    # models inside the process serving HTTP. It did, for one commit, and it did not look like a
    # bug - it looked like a task that failed.
    from prismoid.jobs.dispatcher import ClientDispatcher
    from prismoid.operations.dispatch import set_dispatcher

    set_dispatcher(ClientDispatcher())
    logfire.info("api: dispatcher wired (enqueue-only - the worker owns all machine work)")
    if not settings.push.enabled():
        # Said once, here: push serves the cloud deployment, which sets the key; anywhere else it is off.
        print(
            "api: push notifications are off: PRISMOID_PUSH_CREDENTIALS_JSON is unset (ADR-100)", flush=True
        )
        logfire.info("api: push notifications are off: PRISMOID_PUSH_CREDENTIALS_JSON is unset (ADR-100)")

    # The dispatcher stamps every turn with this process's application version, and a worker runs
    # only its own - so a live worker of another version is said out loud here, where
    # whoever started this process is looking. Never fatal: see `health.version_skew`.
    from prismoid.db.session import session_scope
    from prismoid.operations import health

    try:
        async with session_scope(platform=True) as session:  # the beat is a platform row
            skew = await health.version_skew(session)
    except Exception:
        logfire.exception("api: the worker-version check failed; booting anyway")
    else:
        if skew is not None:
            logfire.warn("api: version skew - {skew}", skew=skew)
            if stdout_log.log_format() != "json":  # the JSON line above is already on stdout
                print(f"WARNING api: version skew - {skew}", file=sys.stderr, flush=True)

    yield

    # Outbound product email rides fire-and-forget tasks (operations/auth): a shutdown
    # must not eat a reset code whose request was already 202'd. Drain before the pool closes.
    from prismoid.operations import auth as auth_ops

    await auth_ops.flush_outbound_email()
    await hub.stop()  # close the process-wide LISTEN connection before the pool goes (it was leaked)
    await dispose_engine()


_globally_instrumented = False


def configure_observability() -> None:
    """One instrumentation story. Logfire is a no-op without a token, and OTLP dual-export
    is a single env var - sovereignty without a second SDK. In json log format (ADR-101) the
    console prints nothing and stdout carries log records only, one JSON object per line."""
    json_logs = stdout_log.install(settings.observability.service_name)
    logfire.configure(
        service_name=settings.observability.service_name,
        environment=settings.environment,
        token=settings.observability.logfire_token.get_secret_value() or None,
        send_to_logfire="if-token-present",
        scrubbing=logfire.ScrubbingOptions(
            # `cs_…` and `ck_…` are credentials. They arrive in a query string on the stream
            # (the one concession to EventSource), so they must never reach a span.
            # The one list both processes scrub lives in `prismoid/observability.py`.
            extra_patterns=SCRUB_PATTERNS,
        ),
        # ...but the scrubber never touches URL attributes (they are its safe keys), and the ASGI
        # server span is born with the query in them: `token=`/`ticket=` values are rewritten as
        # the span starts, ahead of every other processor (logfire's pending span included).
        additional_span_processors=[UrlCredentialSpanProcessor(), *json_logs],
        console=False if json_logs else None,
    )
    # uvicorn logs every WebSocket's path WITH its query (`?token=cs_…`, `?ticket=st_…`) on
    # `uvicorn.error`, access log or not: the values are redacted before any handler sees them.
    install_log_redaction()

    # The rest of the story: host vitals, every asyncpg round-trip, every outbound httpx call -
    # the spans the dashboard reads. No-ops without an exporter. These instrument PROCESS-wide
    # state (unlike instrument_fastapi's per-app wrap), and tests build many apps, so: once.
    global _globally_instrumented
    if not _globally_instrumented:
        logfire.instrument_system_metrics()
        logfire.instrument_asyncpg()
        logfire.instrument_httpx()  # a Telegram token in a URL path: UrlCredentialSpanProcessor
        _globally_instrumented = True


def create_app() -> FastAPI:
    configure_observability()

    # No `default_response_class=ORJSONResponse`. We want the JSON fast path; in FastAPI
    # 0.139 the fast path *is* the default - it serialises a typed return straight to JSON bytes
    # through Pydantic, which is faster than routing it through orjson, and ORJSONResponse is
    # deprecated for exactly that reason. Our views all declare their return type, so we already
    # take it. (ADR-006.)
    app = FastAPI(
        title="Prismoid",
        # The contract's own version, the one X-Prismoid-Protocol answers: a release bumps the product
        # version (prismoid.__version__) and leaves the frozen contract, and so the web's types, alone.
        version=PROTOCOL_VERSION,
        description="A general-purpose digital worker that verifies before it delivers.",
        lifespan=lifespan,
    )

    # Inside CORS, so a 413 still carries the headers a browser needs to read it.
    app.add_middleware(middleware.BodySizeLimit)
    # CORS is installed FIRST so it is the OUTERMOST user middleware - it must answer the browser's
    # preflight (OPTIONS) before auth/logging run. Every authenticated call from the SPA carries a
    # Bearer header, which is never a "simple" request, so each one is preceded by a preflight that
    # must succeed or the real request never fires.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[REQUEST_ID_HEADER, PROTOCOL_HEADER],
    )
    middleware.install(app)
    app.add_middleware(middleware.QuietProbe)  # outermost of ours: a probe's request makes no span
    errors.install(app)
    urls.mount(app)
    if settings.local.web_dir is not None:
        spa.mount(app, settings.local.web_dir)  # last: a catch-all must not shadow a real route

    _make_asgi_span_naming_preflight_safe()  # ADR-006 - must precede instrumentation
    logfire.instrument_fastapi(
        app,
        capture_headers=False,  # headers carry the bearer token
        # instrument_fastapi records resolved endpoint arguments, the body included: dropped on
        # every route (`drop_route_arguments`), since a goal or an email is no secret-shaped key.
        request_attributes_mapper=drop_route_arguments,
        # The middleware sets the URL attributes again after the span starts (undoing the
        # processor's redaction); this hook runs next, so the finished span is redacted too.
        server_request_hook=redact_server_span,
        excluded_urls=PROBE_URLS,
    )
    return app


def drop_route_arguments(request: Request | WebSocket, attributes: dict[str, Any]) -> dict[str, Any]:
    """No route's parsed arguments reach a span. `instrument_fastapi` records them, the request body
    included: a task's goal and its attachments, a channel line, an invite's email, a pasted
    connector token. The scrubber matches secret-shaped keys and values, which a goal or an address
    is not, and an allowlist of sensitive routes misses the next one; the route and its status still
    name the span. A refused body is recorded as `errors`, and each pydantic error carries its
    `input` (a too-short password, a mistyped address, the whole body when a field is missing), so
    only where each failed and why are kept."""
    attributes["values"] = {}
    attributes["errors"] = [
        {"type": e.get("type"), "loc": e.get("loc")} for e in attributes.get("errors", [])
    ]
    return attributes


_preflight_span_naming_patched = False


def _make_asgi_span_naming_preflight_safe() -> None:
    """Work around opentelemetry-instrumentation-fastapi 0.63b1.

    Its route resolver guards the `.path` lookup for a FULL route match but *not* for a PARTIAL one. A
    CORS preflight - `OPTIONS` against a method-specific route - matches PARTIAL, and under FastAPI
    0.139 the matched object is an `_IncludedRouter`, which has no `.path`. So naming the span raises
    `AttributeError`, the ASGI instrumentation (which wraps *outside* every user middleware, CORS
    included) 500s the preflight, and because a Bearer request is never "simple", that silently breaks
    *all* authenticated browser traffic. We wrap the resolver so an unresolvable route falls back to the
    raw path instead of crashing. Idempotent; remove when upstream guards the PARTIAL branch too.
    """
    global _preflight_span_naming_patched
    if _preflight_span_naming_patched:
        return

    import opentelemetry.instrumentation.fastapi as otel_fastapi

    original = getattr(otel_fastapi, "_get_route_details", None)
    if original is None:
        return

    def safe(scope: object) -> object:
        try:
            return original(scope)
        except AttributeError:
            return scope.get("path") if isinstance(scope, dict) else None

    # A deliberate monkey-patch of the instrumentor's private route-namer (the whole point here).
    otel_fastapi._get_route_details = safe  # pyright: ignore[reportPrivateUsage]
    _preflight_span_naming_patched = True


app = create_app()
