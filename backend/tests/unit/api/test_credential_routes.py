# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""No route's parsed arguments reach a span: `instrument_fastapi` records them, the request body
included, and `drop_route_arguments` drops them on every route. The connector create and connect
bodies carry `bearer_token` and `client_secret`, the OAuth callback's query the `cn_` state and the
authorization code, and a task's body its goal and attachments. The routes are the MOUNTED ones,
found by their views, so a moved path fails here, not in production."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.routing import APIRoute

from prismoid.api import app as app_module
from prismoid.api.app import create_app
from prismoid.api.endpoints import connectors as views
from prismoid.api.endpoints import tasks as task_views


def _api_routes(routes: Iterable[Any]) -> list[APIRoute]:
    found: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        for attr in ("routes", "original_router"):
            child = getattr(route, attr, None)
            nested = getattr(child, "routes", child)
            if nested:
                found.extend(_api_routes(nested))
    return found


@pytest.mark.parametrize(
    "view",
    [views.create_connector, views.connect_connector, views.oauth_callback, task_views.create_task],
    ids=["create", "connect", "oauth-callback", "task-create"],
)
def test_no_routes_arguments_reach_a_span(view: Callable[..., Any]) -> None:
    (route,) = {r.path: r for r in _api_routes(create_app().routes) if r.endpoint is view}.values()
    request = SimpleNamespace(scope={"route": route})
    attributes = {"values": {"body": {"bearer_token": "ghp_pasted_secret", "goal": "my plans"}, "code": "c"}}
    dropped = app_module.drop_route_arguments(request, attributes)  # pyright: ignore[reportArgumentType]
    assert dropped["values"] == {}, route.path


def test_a_validation_error_reaches_a_span_as_its_shape_only() -> None:
    """A refused body is recorded as `errors`, and each pydantic error carries its `input`: a
    signup's too-short password, a mistyped address, a whole task body when a field is missing.
    Only where it failed and why reach the span."""
    request = SimpleNamespace(scope={})
    attributes = {
        "values": {},
        "errors": [
            {
                "type": "string_too_short",
                "loc": ["body", "password"],
                "msg": "too short",
                "input": "hunter2-reused",
            },
            {
                "type": "missing",
                "loc": ["body", "goal"],
                "msg": "required",
                "input": {"email": "a@b.example"},
            },
        ],
    }
    dropped = app_module.drop_route_arguments(request, attributes)  # pyright: ignore[reportArgumentType]
    assert dropped["errors"] == [
        {"type": "string_too_short", "loc": ["body", "password"]},
        {"type": "missing", "loc": ["body", "goal"]},
    ]
    assert "hunter2" not in repr(dropped) and "a@b.example" not in repr(dropped)
