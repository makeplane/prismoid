# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The projects list at scale: newest first, keyset pages, name search - the picker's
contract. Hundreds of projects must never mean loading hundreds of projects.

`created_at` is `func.now()` - transaction time - so rows born in this one test transaction
would all tie (and `session_scope` rolls back at exit, so multiple transactions are not an
option here). The test assigns distinct timestamps explicitly: production creates arrive as
separate requests, and distinct timestamps are the shape being modelled.
"""

from __future__ import annotations

import datetime as dt

import pytest

from prismoid.db.session import session_scope
from prismoid.operations import projects as ops
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def test_newest_first_keyset_and_search() -> None:
    async with session_scope() as session:
        actor, _ = await member(session, "Pia")
        await session.flush()

        base = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        rows = []
        for i in range(5):
            rows.append((await ops.create(session, actor, name=f"probe-{i}", budget_usd=None)).project)
        for i, project in enumerate(rows):
            project.created_at = base + dt.timedelta(seconds=i)
        await session.flush()

        first = await ops.list_projects(session, actor, limit=2)
        assert [r.project.name for r in first] == ["probe-4", "probe-3"], "newest first"

        second = await ops.list_projects(session, actor, limit=2, cursor=first[-1].project.cursor())
        assert [r.project.name for r in second] == ["probe-2", "probe-1"], (
            "the keyset continues without overlap or skips"
        )

        hits = await ops.list_projects(session, actor, q="ROBE-2")
        assert [r.project.name for r in hits] == ["probe-2"], "case-insensitive name search"

        none = await ops.list_projects(session, actor, q="100%_literal")
        assert none == [], "ILIKE wildcards in the query are literals, not patterns"


async def test_the_list_says_the_callers_real_role() -> None:
    """The list reads the caller's own role per Project - owner, editor or viewer - in ONE extra
    query for the page (it used to say owner-or-viewer, so an editor's picker
    offered nothing and a viewer's Share offered a move the server refuses)."""
    from prismoid.db.models.project_member import ProjectMember
    from prismoid.db.models.workspace import WorkspaceRole

    async with session_scope() as session:
        ada, workspace = await member(session, "Ada")
        bob, _ = await member(session, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
        base = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        owned = (await ops.create(session, bob, name="bobs-own", budget_usd=None)).project
        edits = (await ops.create(session, ada, name="ada-edits", budget_usd=None)).project
        views = (await ops.create(session, ada, name="ada-views", budget_usd=None)).project
        for i, project in enumerate((owned, edits, views)):
            project.created_at = base + dt.timedelta(seconds=i)
        session.add(ProjectMember(project_id=edits.id, actor_id=bob.id, role="editor", added_by=ada.id))
        session.add(ProjectMember(project_id=views.id, actor_id=bob.id, role="viewer", added_by=ada.id))
        await session.flush()

        listed = await ops.list_projects(session, bob)

        assert [(r.project.name, r.role.value) for r in listed] == [
            ("ada-views", "viewer"),
            ("ada-edits", "editor"),
            ("bobs-own", "owner"),
        ]
