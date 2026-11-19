# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every route is covered or exempt, and only the way back in admits a person with no workspace:
the coverage half of the isolation suite, read from the router. The tables are
`tests/support/rigs/workspace_isolation_rig.py`; the walk is `tests/integration/workspaces/test_workspace_isolation.py`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIWebSocketRoute

from prismoid.api.app import create_app
from prismoid.api.deps import require_actor, require_principal
from tests.support.rigs.workspace_isolation_rig import (
    BODIES,
    CROSS_REF_ROUTES,
    EXEMPT,
    OWN_ONLY,
    PARAM_KEYS,
    REFUSAL,
    WS_ROUTES,
    api_routes,
    endpoints,
    iso_params,
    key_for,
)

pytestmark = pytest.mark.integration


def test_every_route_is_covered_or_exempt() -> None:
    """The router is the list: a route the walk cannot exercise fails HERE, by name."""
    holes: list[str] = []
    for method, path, route in endpoints():
        key = (method, path)
        if key in EXEMPT:
            continue
        params = iso_params(route)
        missing = {p for p in params if key_for(path, p) not in PARAM_KEYS}
        if missing:
            holes.append(f"{method} {path}: no seeded value for {sorted(missing)}")
        if params and route.body_field is not None and key not in BODIES:
            holes.append(f"{method} {path}: takes a body and has no entry in BODIES")
        if not params and method != "GET" and key not in OWN_ONLY and key not in CROSS_REF_ROUTES:
            holes.append(f"{method} {path}: a write with no CROSS_REFS probe and no OWN_ONLY reason")
    for route in api_routes(create_app().routes):
        if isinstance(route, APIWebSocketRoute) and route.path not in WS_ROUTES:
            holes.append(f"WS {route.path}: a WebSocket route the suite does not probe")
    stale = [
        k for k in (*EXEMPT, *OWN_ONLY, *REFUSAL, *BODIES) if k not in {(m, p) for m, p, _ in endpoints()}
    ]
    holes.extend(f"{m} {p}: named in the suite but no longer a route" for m, p in stale)
    assert not holes, "routes the isolation suite cannot walk:\n" + "\n".join(holes)


# The way back in: the only routes a person with no workspace left may reach.
_ORGLESS = {
    ("GET", "/v1/auth/me"),
    ("POST", "/v1/auth/logout"),
    ("GET", "/v1/workspaces"),
    ("POST", "/v1/workspaces"),
    ("POST", "/v1/workspaces/invites/accept"),
}


def _depends_on(dependant: Dependant, call: Callable[..., Any]) -> bool:
    return any(d.call is call or _depends_on(d, call) for d in dependant.dependencies)


def test_only_the_way_back_in_admits_a_person_with_no_workspace() -> None:
    """Every credentialed route asks `require_workspace` through `require_actor` but these: whatever a
    route takes from the door, an orgless credential reaches these routes, no more (admin's too)."""
    orgless = {
        (method, route.path)
        for route in api_routes(create_app().routes)
        if isinstance(route, APIRoute)
        and _depends_on(route.dependant, require_principal)
        and not _depends_on(route.dependant, require_actor)
        for method in (route.methods or set()) - {"HEAD", "OPTIONS"}
    }
    assert orgless == _ORGLESS
