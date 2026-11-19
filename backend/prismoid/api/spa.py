# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The built web app, served by the API itself in local mode (ADR-077): no second web server.

Mounted AFTER the routers and only when `settings.local.web_dir` is set. Real files are served as they
are; any other GET that is not the API's or the docs' falls back to `index.html` so a deep link
survives a reload. An unknown `/v1/...` is never answered here - the API's JSON 404 stands."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response

from prismoid.errors import NotFound

_API_PREFIXES = ("v1", "healthz", "docs", "redoc", "openapi.json")
_HEADERS = {"X-Content-Type-Options": "nosniff"}


def mount(app: FastAPI, web_dir: Path) -> None:
    root = web_dir.resolve()

    async def spa(path: str, request: Request) -> Response:
        # Every method is caught so an unknown API route stays a 404, not a 405 from a GET-only route.
        if request.method != "GET" or path.split("/", 1)[0] in _API_PREFIXES:
            raise NotFound("no such route")
        target = (root / path).resolve()
        if path and target.is_file() and target.is_relative_to(root):
            return FileResponse(target, headers=_HEADERS)
        return FileResponse(root / "index.html", headers={**_HEADERS, "Cache-Control": "no-cache"})

    app.add_api_route(
        "/{path:path}", spa, methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False
    )
