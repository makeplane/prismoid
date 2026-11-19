# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A Bearer request from the browser is never a "simple" request, so every authenticated call the SPA
makes is preceded by a CORS preflight (`OPTIONS`). That preflight must return 200 with the CORS headers
- if it 500s, the browser never sends the real request and the whole app is dark to a signed-in user,
with nothing in the response body to explain why.

Regression guard for ADR-006: opentelemetry-instrumentation-fastapi 0.63b1 crashed naming the span for
an `OPTIONS` that matches a route *partial* (FastAPI 0.139's `_IncludedRouter` has no `.path`), so the
instrumentation - which wraps outside every user middleware, CORS included - turned every preflight
into a 500. `create_app` installs a resolver shim; this test keeps it installed.
"""

from __future__ import annotations

import httpx

from prismoid.api.app import create_app
from prismoid.conf import settings


async def test_bearer_route_preflight_returns_200_not_500() -> None:
    app = create_app()
    origin = settings.api.cors_origins[0]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.options(
            "/v1/auth/me",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )

    # The bug produced a raw 500 here; a healthy preflight is 200 and echoes the allowances.
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == origin
    assert "authorization" in response.headers.get("access-control-allow-headers", "").lower()


async def test_mutating_route_preflight_is_allowed() -> None:
    app = create_app()
    origin = settings.api.cors_origins[0]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.options(
            "/v1/tasks",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == origin
