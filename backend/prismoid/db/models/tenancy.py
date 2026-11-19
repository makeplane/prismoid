# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The tenancy class of every table (ADR-061) - a standing rule, checked by
tests/laws/test_tenancy_map.py. A new table cannot land without deciding its tenancy."""

from __future__ import annotations

from enum import StrEnum


class TenancyClass(StrEnum):
    ROOT = "tenant root"  # workspace_id NOT NULL; fetchers and lists filter on it
    STAMP = "ledger stamp"  # workspace_id copied from the parent at write time
    PLATFORM_OR_WORKSPACE = "platform or workspace"  # workspace_id NULL = platform row
    CHILD = "tenant child"  # reached only through its root
    PLATFORM = "platform"  # no workspace
    IDENTITY = "identity"  # a person is global; membership is the join
    WORKSPACE = "workspace"  # the tenant tables themselves


_ROOTS = (
    "projects",
    "project_invites",
    "tasks",
    "machines",
    "device_grants",
    "conversations",
    "bots_registry",
    "bot_channels",
    "routines",
    "connectors",
    "egress_allowances",
    "captures",
    "inbox_items",
    "memories",
    "plugin_installs",
    "bot_template_installs",
    "api_keys",
)

CHILD_PATHS: dict[str, str] = {
    "plan_steps": "tasks",
    "plan_edges": "plan_steps → tasks",
    "plan_edits": "plan_steps → tasks",
    "call_log": "plan_steps → tasks",
    "steers": "tasks",
    "gates": "tasks",
    "artifacts": "tasks",
    "verifications": "artifacts → tasks",
    "messages": "conversations",
    "routine_runs": "routines",
    "routine_event_cursor": "routines",
    "memory_uses": "memories",
    "project_members": "projects",
    "project_usage_daily": "projects",
}

TABLE_CLASSES: dict[str, TenancyClass] = {
    **dict.fromkeys(_ROOTS, TenancyClass.ROOT),
    "task_events": TenancyClass.STAMP,
    "usage_events": TenancyClass.STAMP,
    "skills_registry": TenancyClass.PLATFORM_OR_WORKSPACE,
    **dict.fromkeys(CHILD_PATHS, TenancyClass.CHILD),
    **dict.fromkeys(
        (
            "price_book",
            "model_catalog",
            "routing_table",
            "system_beats",
            "admin_actions",
            "bot_templates",
            "features",
            "auth_throttles",
        ),
        TenancyClass.PLATFORM,
    ),
    **dict.fromkeys(
        (
            "actors",
            "users",
            "credentials",
            "password_resets",
            "email_verifications",
            "auth_sessions",
            "push_devices",
            "stream_tickets",
        ),
        TenancyClass.IDENTITY,
    ),
    **dict.fromkeys(("workspaces", "workspace_members", "workspace_invites"), TenancyClass.WORKSPACE),
}
