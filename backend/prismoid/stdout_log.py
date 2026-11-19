# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""stdout in staging and prod: one JSON object per log record, never a span (ADR-101).

python-json-logger writes the lines; `TraceContext` is the usual trace-context filter plus
Datadog's `dd.*` ids and the scrub; `LogfireRecords` hands Logfire's log events to it."""

from __future__ import annotations

import logging
import re
import sys
from typing import IO, Any, Final

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from pythonjsonlogger import json as jsonlogger
from pythonjsonlogger.core import RESERVED_ATTRS

from prismoid.conf import settings
from prismoid.conf_observability import LogFormat
from prismoid.observability import REDACTED, SCRUB_PATTERNS, redact_query_credentials

OWN_LOGGERS: Final = ("prismoid", "uvicorn", "dbos")  # INFO is ours; other libraries stay at WARNING
_FROM_LOGFIRE: Final = ((17, logging.ERROR), (13, logging.WARNING), (9, logging.INFO), (0, logging.DEBUG))
_SECRET_VALUE: Final = re.compile("|".join(f"(?:{p})" for p in SCRUB_PATTERNS))
_SECRET_KEY: Final = re.compile(r"password|secret|token|api[-_]?key|authoriz|cookie|credential|session", re.I)
_handler: list[logging.Handler] = []


def log_format() -> LogFormat:
    """`PRISMOID_LOG_FORMAT`, else json where a log pipeline reads stdout (staging, prod)."""
    chosen = settings.observability.log_format
    return chosen or ("json" if settings.environment in ("staging", "prod") else "console")


def _floor() -> int:
    return logging.getLevelNamesMapping()[settings.observability.log_level.upper()]


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: REDACTED if _SECRET_KEY.search(str(k)) else scrub(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [scrub(v) for v in value]
    if value is None or isinstance(value, bool | int | float):
        return value
    return _SECRET_VALUE.sub(REDACTED, redact_query_credentials(str(value)))


class TraceContext(logging.Filter):
    """The ids of the span a record was written in (hex, and Datadog's decimal low 64 bits), the
    service tags, the exception as `error.*`; message and extras scrubbed."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.tags = {
            "dd.service": service,
            "dd.env": settings.environment,
            "dd.version": settings.worker.app_version,
        }

    def filter(self, record: logging.LogRecord) -> bool:
        ctx, fields = trace.get_current_span().get_span_context(), record.__dict__
        if "trace_id" not in fields and ctx.is_valid:
            fields |= {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}
        if fields.get("trace_id"):
            fields["dd.trace_id"] = str(int(fields["trace_id"], 16) & 0xFFFF_FFFF_FFFF_FFFF)
            fields["dd.span_id"] = str(int(fields["span_id"], 16))
        if record.exc_info and (exc := record.exc_info[1]) is not None:
            stack = logging.Formatter().formatException(record.exc_info)
            fields |= {"error.kind": type(exc).__name__, "error.message": str(exc), "error.stack": stack}
            record.exc_info = record.exc_text = None
        record.msg, record.args = record.getMessage(), None
        fields.pop("color_message", None)  # uvicorn's ANSI copy of the message
        for key, value in list(fields.items()):
            if key not in RESERVED_ATTRS or key == "msg":
                fields[key] = REDACTED if _SECRET_KEY.search(key) and isinstance(value, str) else scrub(value)
        fields |= self.tags
        return True


class LogfireRecords(SpanProcessor):
    """Logfire's log events (zero-length spans) as records on the JSON handler; other spans: OTLP only."""

    def on_end(self, span: ReadableSpan) -> None:
        attrs = dict(span.attributes or {})
        num = attrs.get("logfire.level_num", 9)
        if attrs.get("logfire.span_type") != "log" or not _handler or not isinstance(num, int):
            return
        level = next(lv for floor, lv in _FROM_LOGFIRE if num >= floor)
        if level < _floor():
            return
        path = str(attrs.get("code.filepath") or "")
        name = path.removesuffix(".py").replace("/", ".") or "logfire"
        record = logging.LogRecord(name, level, path, 0, str(attrs.get("logfire.msg", span.name)), None, None)
        record.created = (span.start_time or 0) / 1e9
        record.__dict__ |= {k: v for k, v in attrs.items() if not k.startswith(("logfire.", "code."))}
        for event in (e for e in span.events if e.name == "exception"):
            e = dict(event.attributes or {})
            parts = {"kind": "type", "message": "message", "stack": "stacktrace"}
            record.__dict__ |= {f"error.{k}": e.get(f"exception.{a}", "") for k, a in parts.items()}
        if span.parent is not None:  # the span it was written in, not its own zero-length one
            record.trace_id, record.span_id = (
                format(span.parent.trace_id, "032x"),
                format(span.parent.span_id, "016x"),
            )
        _handler[0].handle(record)


def install(service: str, *, stream: IO[str] | None = None) -> list[SpanProcessor]:
    """json format: one JSON handler on the root logger, every logger that prints on its own (uvicorn's,
    the durable runtime's) handing it its records, and a processor for Logfire's events (the caller
    turns Logfire's console off). console format: nothing. Idempotent."""
    if log_format() != "json":
        return []
    uninstall()
    handler = logging.StreamHandler(stream or sys.stdout)
    rename = {"levelname": "status", "name": "logger.name"}
    fmt = jsonlogger.JsonFormatter("%(levelname)s %(name)s %(message)s", rename_fields=rename, timestamp=True)
    handler.setFormatter(fmt)
    handler.addFilter(TraceContext(service))
    _handler.append(handler)
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(max(_floor(), logging.WARNING))
    for name in OWN_LOGGERS:
        logging.getLogger(name).setLevel(_floor())
    logging.captureWarnings(True)
    durable = logging.getLogger("dbos")  # adds its own console handler only to a logger with none
    if not durable.handlers:
        durable.addHandler(logging.NullHandler())
    adopt_stream_handlers()
    return [LogfireRecords()]


def uninstall() -> None:
    for old in _handler:
        logging.getLogger().removeHandler(old)
    _handler.clear()
    logging.captureWarnings(False)


def adopt_stream_handlers() -> None:
    """Loggers with their own terminal handler drop it and propagate to the JSON handler (their filters,
    the query-credential redaction among them, still run first). A no-op before `install`."""
    terminals = {sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__}
    for logger in [logging.getLogger(n) for n in list(logging.root.manager.loggerDict)] if _handler else []:
        mine = [h for h in logger.handlers if type(h) is logging.StreamHandler and h.stream in terminals]
        for h in mine:
            logger.removeHandler(h)
        logger.propagate = logger.propagate or bool(mine)
