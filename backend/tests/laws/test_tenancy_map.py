# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every table has exactly one tenancy class (ADR-061)."""

from __future__ import annotations

from prismoid.db.base import Base
from prismoid.db.models import ALL_TABLES
from prismoid.db.models.tenancy import CHILD_PATHS, TABLE_CLASSES, TenancyClass


def test_every_table_is_classified() -> None:
    assert set(TABLE_CLASSES) == set(ALL_TABLES), set(ALL_TABLES) ^ set(TABLE_CLASSES)


def test_children_name_their_root_path() -> None:
    children = {t for t, c in TABLE_CLASSES.items() if c is TenancyClass.CHILD}
    assert children == set(CHILD_PATHS)


def test_roots_and_stamps_carry_workspace_id() -> None:
    for name, cls in TABLE_CLASSES.items():
        table = Base.metadata.tables[name]
        if cls in (TenancyClass.ROOT, TenancyClass.STAMP, TenancyClass.PLATFORM_OR_WORKSPACE):
            assert "workspace_id" in table.c, f"{name} is {cls} but has no workspace_id"
        if cls in (TenancyClass.PLATFORM, TenancyClass.CHILD):
            assert "workspace_id" not in table.c, f"{name} is {cls} but carries workspace_id"


def test_roots_and_task_events_are_not_null() -> None:
    for name, cls in TABLE_CLASSES.items():
        if cls is TenancyClass.ROOT or name == "task_events":
            assert Base.metadata.tables[name].c.workspace_id.nullable is False, name


def test_project_references_are_composite() -> None:
    # The spec's seven, plus memories: a tenant root that points at a project too,
    # plus project_invites: a join link can never open another workspace's project.
    for name in (
        "tasks",
        "conversations",
        "bots_registry",
        "bot_channels",
        "routines",
        "captures",
        "inbox_items",
        "memories",
        "project_invites",
    ):
        fks = {tuple(fk.column_keys) for fk in Base.metadata.tables[name].foreign_key_constraints}
        assert ("project_id", "workspace_id") in fks, name


def test_no_model_gives_workspace_id_a_server_default() -> None:
    """No model declares a server default for `workspace_id`: one would let the ORM omit a None
    workspace, and the database would quietly file platform spend or a builtin under a tenant.
    The code stamps every workspace itself."""
    for name in TABLE_CLASSES:
        for column in ("workspace_id", "active_workspace_id"):
            col = Base.metadata.tables[name].c.get(column)
            assert col is None or col.server_default is None, f"{name}.{column}"
