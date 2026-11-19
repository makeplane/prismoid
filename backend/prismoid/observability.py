# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What both processes agree is a secret: the scrub patterns the API and the worker
hand logfire, and the span processor that keeps a credential out of a URL span. ONE home, so the
invariant cannot hold in only one of the two places it must.

The API's own half lives here too: the query-string credentials - `ticket=` (the screen socket's
single-use `st_` admission and the event streams' single-use `et_` one), `token=` (no route reads a
credential there any more, but an old client may still send its long-lived `cs_`/`ck_` one) and
`sig=` (the local blob door's HMAC), and the
connectors OAuth callback's `state=` (its `cn_` authentication) and `code=` (the authorization
code the provider's tokens are bought with) - are rewritten to `[redacted]` before they reach uvicorn's log lines (`QueryCredentialFilter`) or a span's URL attributes - as it starts
(`UrlCredentialSpanProcessor`: logfire's pending span) and once the ASGI middleware has set them
again (`redact_server_span`: the finished span). Logfire never pattern-scrubs URL attributes (they are safe keys), and
uvicorn logs every WebSocket's path WITH its query on `uvicorn.error`, access log or not."""

from __future__ import annotations

import contextlib
import logging
import re
import urllib.parse
from collections.abc import Mapping
from typing import Any

from opentelemetry.context import Context
from opentelemetry.sdk.trace import Span, SpanProcessor

# `cs_`/`ck_` credentials, `cn_` connector OAuth state, `cr_` password-reset codes, `ev_` address-verification codes,
# `rt_` routine fire tokens, `bc_` channel tokens, `oi_` workspace invite codes,
# `si_` Project join links, `et_` stream tickets (anchored at a word start, so an identifier
# such as `get_…` is left alone), `gAAAAA…` Fernet ciphertext, and a Telegram BotFather
# token (`123456:ABC-…`) - same rule, same fate.
SCRUB_PATTERNS = [
    r"(?:c[sknr]|rt|bc|oi|si)_[A-Za-z0-9_\-]{20,}",
    r"\b(?:et|ev)_[A-Za-z0-9_\-]{40,}",
    r"gAAAAA[A-Za-z0-9_\-=]{20,}",
    r"\b\d{6,}:[A-Za-z0-9_\-]{30,}\b",
]

# Telegram's API shape puts the BotFather token in the URL PATH (`/bot<token>/getUpdates`), and the
# scrubber never touches URL attributes: `_redact_url_attributes` rewrites it with the query-string
# credentials, as the span starts.
_TELEGRAM_PATH = re.compile(r"/bot\d+:[^/]+/")


# ── query-string credentials (the API's log lines and its server spans) ──────────────────────

# `state` and `code` are named by no other route: redacting them everywhere costs no request line.
CREDENTIAL_PARAMS = frozenset({"token", "ticket", "sig", "state", "code"})
REDACTED = "[redacted]"
# One `name=value` pair: at the start of a bare query, or after `?` or `&` in a URL or a log
# line. A value ends at the next pair, a fragment, whitespace or a quote.
_PAIR = re.compile(r"(?P<lead>^|[?&])(?P<name>[^=&?#\s\"']+)=(?P<value>[^&#\s\"']*)")
_URL_ATTRIBUTES = ("http.url", "http.target", "url.full", "url.path", "url.query")
# The load balancer's probe: a request every few seconds that is never worth a trace (ADR-101).
PROBE_PATHS = frozenset({"/healthz"})
PROBE_URLS = [rf"{path}$" for path in sorted(PROBE_PATHS)]  # OTel's excluded_urls: searched in the URL
_UVICORN_LOGGERS = ("uvicorn.error", "uvicorn.access")


def redact_query_credentials(text: str) -> str:
    """`text` - a URL, a path with its query, a bare query or a log line - with the value of every
    credential parameter (`CREDENTIAL_PARAMS`) replaced by `[redacted]`; everything else as it was.
    A name is compared decoded (`tok%65n=` is the `token` Starlette reads)."""
    if "=" not in text:
        return text

    def pair(match: re.Match[str]) -> str:
        if urllib.parse.unquote_plus(match["name"]) not in CREDENTIAL_PARAMS:
            return match[0]
        return f"{match['lead']}{match['name']}={REDACTED}"

    return _PAIR.sub(pair, text)


class QueryCredentialFilter(logging.Filter):
    """A logger filter that redacts query-string credentials from a record before any handler
    formats it. Each string argument is redacted in place (uvicorn's access formatter unpacks the
    argument tuple, so its shape is kept). The message is always redacted too: a record without
    arguments (an f-string line: its `args` is `()`) has its text redacted, and a template that
    still carries a credential once formatted - its own text, or a placeholder an argument fills
    (`?token=%s`) - is flattened to its redacted line."""

    def filter(self, record: logging.LogRecord) -> bool:
        args: Any = record.args
        if isinstance(args, tuple):
            record.args = tuple(_redacted(arg) for arg in args)
        elif isinstance(args, Mapping):
            record.args = {key: _redacted(value) for key, value in args.items()}
        if not isinstance(record.msg, str):
            return True
        if not record.args:
            record.msg = redact_query_credentials(record.msg)
            return True
        try:
            line = record.getMessage()
        except Exception:  # a template that does not format: the handler reports it as ever
            return True
        if (clean := redact_query_credentials(line)) != line:
            record.msg, record.args = clean, ()
        return True


def _redacted(value: object) -> object:
    return redact_query_credentials(value) if isinstance(value, str) else value


def install_log_redaction() -> None:
    """Put `QueryCredentialFilter` on the loggers uvicorn writes request lines to: `uvicorn.error`
    (every WebSocket's `"WebSocket <path>?<query>" [accepted]`, whatever `access_log` says) and
    `uvicorn.access`. Idempotent; uvicorn has configured logging before it imports the app, so a
    filter added at app creation stays."""
    for name in _UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(existing, QueryCredentialFilter) for existing in logger.filters):
            logger.addFilter(QueryCredentialFilter())


class UrlCredentialSpanProcessor(SpanProcessor):
    """Redacts a span's URL attributes as it STARTS (`_redact_url_attributes`), server and client
    spans alike, in the API and the worker. Logfire emits a pending span - and builds its message,
    `HTTP /path ? token='…'` or `GET api.telegram.org/bot<token>/…`, from the URL - as the span
    starts, before any request hook runs (and httpx's `request_hook` never runs for an
    AsyncClient, which is why the old hook leaked every Telegram token); installed first
    among the span processors (`logfire.configure(additional_span_processors=...)`), this runs
    ahead of it."""

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        _redact_url_attributes(span)


def redact_server_span(span: Any, scope: Mapping[str, Any]) -> None:
    """The ASGI server span's request hook (`instrument_fastapi(server_request_hook=...)`): the
    OTel middleware sets the request's attributes AGAIN right after the span starts - undoing the
    processor's redaction - and calls this hook next, so the finished span is redacted here."""
    if scope.get("query_string"):
        _redact_url_attributes(span)


def _redact_url_attributes(span: Any) -> None:
    """`http.url`, `http.target`, `url.full`, `url.path` and `url.query`, each carrying a credential
    parameter or a Telegram bot path, rewritten in place. The scrubber never touches them: URL
    attributes are its safe keys."""
    attributes: Any = getattr(span, "attributes", None) or {}
    for key in _URL_ATTRIBUTES:
        value = attributes.get(key)
        if not isinstance(value, str):
            continue
        clean = _TELEGRAM_PATH.sub("/bot[redacted]/", redact_query_credentials(value))
        if clean != value:
            with contextlib.suppress(Exception):  # a span that refuses is not worth a request
                span.set_attribute(key, clean)
