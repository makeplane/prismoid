# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The API surface, closed (promoted by ADR-008).

Phase 1 shipped every route: a `501` left anywhere in `api/` is a promise the surface no longer
makes. And the OpenAPI spec must carry every route except the one deliberate exception - `/watch`
is an HTML page, not part of the contract a client generates types from. A route missing from the
spec is a route the frontend cannot call; a spec entry without a route is a lie.
"""

from __future__ import annotations

import re
from pathlib import Path

from prismoid import api
from prismoid.api.app import create_app

API_DIR = Path(api.__file__).parent


def test_no_501_stubs_remain() -> None:
    offenders = [p.name for p in API_DIR.rglob("*.py") if "501" in p.read_text()]
    assert not offenders, f"501 is back in api/: {offenders}"


def test_openapi_path_set_matches_the_route_table_but_for_the_watch_page() -> None:
    """SCREEN 1 (urls.py) and the OpenAPI spec must declare the SAME set of paths.

    Count parity (`ops == routes - 1`) was the bug: a route missing from the spec and a phantom
    spec entry cancel out in a count, and the check stays green over a real hole. Path SETS surface
    both directions - and stay robust when an endpoint's *response shape* changes (a typed `*Out`)
    without changing its path, which is the only thing a contract regeneration touches. The one
    allowed gap is `/watch/{task_id}`, an HTML page mounted `include_in_schema=False` - a human
    surface, never part of the client-generated contract; the WebSocket stream carries no OpenAPI
    path either (it is `add_api_websocket_route`, not `add_api_route`).
    """
    spec_paths = set(create_app().openapi()["paths"])

    declared: set[str] = set()
    for line in (API_DIR / "urls.py").read_text().splitlines():
        if "include_in_schema=False" in line:
            continue  # /watch - deliberately outside the typed contract
        router = re.search(r'\br\.add_api_route\(\s*"([^"]+)"', line)  # mounted under the /v1 prefix
        if router:
            declared.add("/v1" + router.group(1))
            continue
        app_route = re.search(r'\bapp\.add_api_route\(\s*"([^"]+)"', line)  # absolute (healthz, SSE)
        if app_route:
            declared.add(app_route.group(1))

    assert declared == spec_paths, (
        "SCREEN 1 (urls.py) and the OpenAPI spec disagree on paths - "
        f"only in urls.py: {sorted(declared - spec_paths)}; "
        f"only in the spec: {sorted(spec_paths - declared)}"
    )
