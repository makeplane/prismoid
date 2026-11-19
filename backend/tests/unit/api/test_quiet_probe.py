# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The load balancer's `/healthz` every few seconds writes no span (ADR-101): `QuietProbe` runs the
request with instrumentation suppressed, its SQL included, and every other path is traced as before."""

from __future__ import annotations

from collections.abc import Iterator

import logfire
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from logfire.testing import TestExporter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from prismoid.api.middleware import QuietProbe


@pytest.fixture
def exporter() -> Iterator[TestExporter]:
    exporter = TestExporter()
    logfire.configure(
        send_to_logfire=False, console=False, additional_span_processors=[SimpleSpanProcessor(exporter)]
    )
    try:
        yield exporter
    finally:
        logfire.configure(send_to_logfire=False, console=False)


def _app() -> FastAPI:
    app = FastAPI()

    async def handler() -> dict[str, str]:
        with logfire.span("SELECT 1"):  # the probe's own query, as the database instrumentation records it
            return {"status": "ok"}

    app.add_api_route("/healthz", handler, methods=["GET"])
    app.add_api_route("/v1/tasks", handler, methods=["GET"])
    app.add_middleware(QuietProbe)
    return app


def test_a_probe_writes_no_span_and_other_requests_still_do(exporter: TestExporter) -> None:
    """Server-side spans only: the test client's own request span (an instrumented httpx, when
    another test turned that on) is the caller's, outside the app."""
    client = TestClient(_app())

    def server_spans() -> list[str]:
        ended = [
            s for s in exporter.exported_spans if (s.attributes or {}).get("logfire.span_type") == "span"
        ]
        return [span.name for span in ended if span.name == "SELECT 1"]

    assert client.get("/healthz").status_code == 200
    assert server_spans() == []
    assert client.get("/v1/tasks").status_code == 200
    assert server_spans() == ["SELECT 1"]
