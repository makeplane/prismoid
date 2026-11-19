# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Tracing for a tool's HTTP calls: one span of facts the tool chooses, never the instrumented spans
whose URL or body carries what the model asked (a query string, a search)."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, cast

import logfire


@contextmanager
def quiet_span(template: str, **facts: str) -> Generator[logfire.LogfireSpan]:
    """One span of `facts` around HTTP calls whose instrumented spans would carry content (a query);
    a failure lands on it as its type, never its message."""
    failure: Exception | None = None
    attributes = cast("dict[str, Any]", facts)
    with logfire.span(template, **attributes) as span, logfire.suppress_instrumentation():
        try:
            yield span
        except Exception as exc:
            span.set_attribute("error", type(exc).__name__)
            span.set_level("error")
            failure = exc
    if failure is not None:
        raise failure
