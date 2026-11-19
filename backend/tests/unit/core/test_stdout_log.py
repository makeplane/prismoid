# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""stdout in json format (ADR-101): one python-json-logger object per log record, never a span -
Logfire's log events and stdlib records alike, scrubbed, at the configured floor, and correlated
with the trace they were written in (Datadog's `dd.trace_id`, the trace id's low 64 bits in decimal)."""

from __future__ import annotations

import io
import json
import logging
import sys
from collections.abc import Callable, Iterator
from typing import Any

import logfire
import pytest
from opentelemetry import trace

from prismoid import stdout_log
from prismoid.conf import settings
from prismoid.observability import SCRUB_PATTERNS, UrlCredentialSpanProcessor

SESSION_TOKEN = "cs_" + "A" * 40
Lines = Callable[[], list[dict[str, Any]]]


Start = Callable[..., Lines]


@pytest.fixture
def start(monkeypatch: pytest.MonkeyPatch) -> Iterator[Start]:
    """`start(level)` installs json format at that floor against a buffer and configures Logfire as
    the processes do; it returns `lines()`, what reached the buffer, parsed. Everything global is
    put back after."""
    root, durable = logging.getLogger(), logging.getLogger("dbos")
    saved = (root.level, {n: logging.getLogger(n).level for n in stdout_log.OWN_LOGGERS})
    saved_durable = (list(durable.handlers), durable.propagate)

    def begin(level: str = "info") -> Lines:
        monkeypatch.setattr(settings.observability, "log_format", "json")
        monkeypatch.setattr(settings.observability, "log_level", level)
        buffer = io.StringIO()
        processors = stdout_log.install("prismoid-test", stream=buffer)
        logfire.configure(
            send_to_logfire=False,
            console=False,
            scrubbing=logfire.ScrubbingOptions(extra_patterns=SCRUB_PATTERNS),
            additional_span_processors=[UrlCredentialSpanProcessor(), *processors],
        )
        return lambda: [json.loads(line) for line in buffer.getvalue().splitlines()]

    try:
        yield begin
    finally:
        stdout_log.uninstall()
        root.setLevel(saved[0])
        for name, level in saved[1].items():
            logging.getLogger(name).setLevel(level)
        durable.handlers[:], durable.propagate = saved_durable
        logfire.configure(send_to_logfire=False, console=False)


@pytest.fixture
def json_logs(start: Start) -> Lines:
    return start()


def test_a_log_event_is_one_json_line_in_its_trace(json_logs: Lines) -> None:
    """A `logfire.info` inside a span is one line with Datadog's fields, its trace, and the span it
    was written in (not its own zero-length log span)."""
    with logfire.span("deliver the task") as span:
        logfire.info("password reset {rid} email sent", rid="r-1")
    context = span.get_span_context()
    assert context is not None
    (line,) = json_logs()
    assert line["message"] == "password reset r-1 email sent"
    assert line["status"] == "INFO" and line["dd.service"] == "prismoid-test" and line["logger.name"]
    assert line["rid"] == "r-1"
    assert line["trace_id"] == format(context.trace_id, "032x")
    assert line["dd.trace_id"] == str(context.trace_id & 0xFFFF_FFFF_FFFF_FFFF)
    assert line["dd.span_id"] == str(context.span_id)
    assert line["timestamp"].endswith("+00:00")


def test_a_record_outside_a_span_has_no_trace_ids(json_logs: Lines) -> None:
    logging.getLogger("prismoid.x").warning("no trace here")
    (line,) = json_logs()
    assert "trace_id" not in line and "dd.trace_id" not in line


def test_a_span_writes_nothing(json_logs: Lines) -> None:
    """A span - Logfire's or another instrumentation's, such as a database round-trip - leaves only
    by OTLP: stdout carries records."""
    with logfire.span("GET /v1/tasks"), trace.get_tracer("asyncpg").start_as_current_span("SELECT 1"):
        pass
    assert json_logs() == []


def test_a_stdlib_record_is_one_json_line(json_logs: Lines) -> None:
    """A `prismoid.*` logger's INFO lands with its logger, level and `extra=` fields; a library's INFO
    stays below the floor (the stdlib default)."""
    logging.getLogger("prismoid.operations.auth").info("sent %s", "mail", extra={"count": 3})
    logging.getLogger("httpx").info("HTTP Request: GET https://example.test")
    (line,) = json_logs()
    assert (line["message"], line["status"]) == ("sent mail", "INFO")
    assert line["logger.name"] == "prismoid.operations.auth"
    assert line["count"] == 3


def test_secrets_never_reach_stdout(json_logs: Lines) -> None:
    """A credential-shaped value, a credential query parameter and a secret-named field are redacted
    on both paths."""
    logfire.info("socket {url}", url=f"/v1/x?ticket=st_{'B' * 40}", api_key="sk-live-123")
    logging.getLogger("prismoid.api").warning(
        "old client sent ?token=%s", SESSION_TOKEN, extra={"password": "p4ss"}
    )
    logging.getLogger("uvicorn.error").info("WebSocket /v1/stream?token=%s [accepted]", SESSION_TOKEN)
    shown = "\n".join(json.dumps(line) for line in json_logs())
    for secret in (SESSION_TOKEN, "st_" + "B" * 40, "sk-live-123", "p4ss"):
        assert secret not in shown, secret
    assert shown.count("[redacted]") >= 4


def test_an_exception_carries_its_kind_message_and_stack(json_logs: Lines) -> None:
    try:
        raise ValueError("the bucket refused the upload")
    except ValueError:
        logfire.exception("upload failed")
        logging.getLogger("prismoid.storage").exception("upload failed")
    lines = json_logs()
    assert [line["status"] for line in lines] == ["ERROR", "ERROR"]
    for line in lines:
        assert line["error.kind"] == "ValueError"
        assert line["error.message"] == "the bucket refused the upload"
        assert "Traceback" in line["error.stack"]


def test_the_floor_drops_less_severe_records(start: Start) -> None:
    lines = start("warning")
    logfire.info("routine tick")
    logfire.warn("version skew")
    logging.getLogger("prismoid.x").info("routine")
    logging.getLogger("prismoid.x").warning("odd")
    assert [line["message"] for line in lines()] == ["version skew", "odd"]


def test_install_is_idempotent(start: Start) -> None:
    start()
    start()
    installed = [
        h
        for h in logging.getLogger().handlers
        if any(isinstance(f, stdout_log.TraceContext) for f in h.filters)
    ]
    assert len(installed) == 1


def test_terminal_handlers_are_adopted(json_logs: Lines) -> None:
    """A logger that prints on its own (uvicorn's, the durable runtime's) hands its records to the
    JSON handler: no second, unparseable line."""
    logger = logging.getLogger("dbos.adopt-test")
    logger.addHandler(logging.StreamHandler(sys.stderr))
    logger.propagate = False
    stdout_log.adopt_stream_handlers()
    logger.warning("recovered 2 workflows")
    assert not logger.handlers and logger.propagate
    assert [line["message"] for line in json_logs()] == ["recovered 2 workflows"]


@pytest.mark.parametrize(
    ("environment", "chosen", "expected"),
    [
        ("prod", None, "json"),
        ("staging", None, "json"),
        ("dev", None, "console"),
        ("local", None, "console"),
        ("dev", "json", "json"),
        ("prod", "console", "console"),
    ],
)
def test_the_format_follows_the_environment_unless_named(
    monkeypatch: pytest.MonkeyPatch, environment: str, chosen: str | None, expected: str
) -> None:
    monkeypatch.setattr(settings, "environment", environment)
    monkeypatch.setattr(settings.observability, "log_format", chosen)
    assert stdout_log.log_format() == expected


def test_console_format_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.observability, "log_format", "console")
    handlers = list(logging.getLogger().handlers)
    assert stdout_log.install("prismoid-test") == []
    assert logging.getLogger().handlers == handlers


def test_the_durable_runtime_never_prints_on_its_own(json_logs: Lines) -> None:
    """The runtime adds its console handler only to a logger without one; json format leaves it a
    placeholder, so the runtime's first line is already JSON."""
    durable = logging.getLogger("dbos")
    assert durable.handlers and durable.propagate
    assert not [h for h in durable.handlers if isinstance(h, logging.StreamHandler)]
    durable.warning("recovering workflows")
    assert [line["logger.name"] for line in json_logs()] == ["dbos"]
