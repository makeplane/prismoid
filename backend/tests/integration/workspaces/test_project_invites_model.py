# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""ProjectInvite: an owner-minted, hashed, TTL'd, single-use join link into a Project (single-use)."""

import asyncio
import datetime as dt
import uuid

import pytest

from prismoid.db.base import utcnow
from prismoid.db.models.project_invite import ProjectInvite
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.session import session_scope
from prismoid.operations import projects as project_ops
from prismoid.schemas.ids import PROJECT_INVITE_PREFIX, hash_token
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def _project_and_owner():
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        project = (await project_ops.create(s, ada, name=f"launch-{uuid.uuid4().hex[:6]}")).project
        await s.commit()
        return ada, workspace, project


async def test_mint_stores_only_the_hash_and_stamps_the_workspace() -> None:
    ada, workspace, project = await _project_and_owner()
    async with session_scope(workspace_id=workspace.id) as s:
        row, raw = await ProjectInvite.mint(
            s, project_id=project.id, role="editor", expires_in_days=7, created_by=ada.id
        )
        await s.commit()
    assert raw.startswith(PROJECT_INVITE_PREFIX)
    assert row.code_hash == hash_token(raw) and raw not in row.code_hash
    assert row.workspace_id == workspace.id and row.uses == 0 and row.state(utcnow()) == "live"
    async with session_scope() as s:
        assert (await ProjectInvite.by_code(s, raw)) is not None  # unfenced: the code is the credential
        assert (await ProjectInvite.by_code(s, raw + "x")) is None


async def test_a_minted_link_is_single_use() -> None:
    """Every link is one-time - the first use spends it and the code is
    dead from then on, for anyone."""
    ada, workspace, project = await _project_and_owner()
    async with session_scope(workspace_id=workspace.id) as s:
        row, raw = await ProjectInvite.mint(
            s, project_id=project.id, role="editor", expires_in_days=7, created_by=ada.id
        )
        await s.commit()
    assert row.max_uses == 1
    async with session_scope(workspace_id=workspace.id) as s:
        assert await ProjectInvite.take_use(s, row.id) is True
        assert await ProjectInvite.take_use(s, row.id) is False
        await s.commit()
    async with session_scope(workspace_id=workspace.id) as s:
        again = await s.get(ProjectInvite, row.id)
        assert again is not None and again.uses == 1 and again.state(utcnow()) == "used_up"
        assert await ProjectInvite.by_code(s, raw) is None
        spent = await ProjectInvite.by_code(s, raw, spent_ok=True)  # the redeem path's re-open read
        assert spent is not None and spent.id == row.id


async def test_the_last_use_has_exactly_one_winner() -> None:
    ada, workspace, project = await _project_and_owner()
    async with session_scope(workspace_id=workspace.id) as s:
        row, _ = await ProjectInvite.mint(
            s, project_id=project.id, role="viewer", expires_in_days=7, created_by=ada.id
        )
        await s.commit()

    async def take() -> bool:
        async with session_scope(workspace_id=workspace.id) as s:
            won = await ProjectInvite.take_use(s, row.id)
            await s.commit()
            return won

    results = await asyncio.gather(take(), take(), take())
    assert sorted(results) == [False, False, True]
    async with session_scope(workspace_id=workspace.id) as s:
        again = await s.get(ProjectInvite, row.id)
        assert again is not None and again.uses == 1 and again.state(utcnow()) == "used_up"


async def test_expired_and_revoked_are_not_live() -> None:
    ada, workspace, project = await _project_and_owner()
    async with session_scope(workspace_id=workspace.id) as s:
        old, old_raw = await ProjectInvite.mint(
            s, project_id=project.id, role="viewer", expires_in_days=1, created_by=ada.id
        )
        old.expires_at = utcnow() - dt.timedelta(seconds=1)
        gone, gone_raw = await ProjectInvite.mint(
            s, project_id=project.id, role="viewer", expires_in_days=1, created_by=ada.id
        )
        gone.revoked_at = utcnow()
        await s.commit()
    async with session_scope(workspace_id=workspace.id) as s:
        assert await ProjectInvite.by_code(s, old_raw) is None
        assert await ProjectInvite.by_code(s, gone_raw) is None
        assert await ProjectInvite.by_code(s, old_raw, spent_ok=True) is None  # dead is dead
        assert await ProjectInvite.by_code(s, gone_raw, spent_ok=True) is None
        assert await ProjectInvite.take_use(s, old.id) is False
        assert await ProjectInvite.take_use(s, gone.id) is False


def test_state_reads_expired_directly() -> None:
    """Revoked outranks used up outranks expired - and an unexpired, unspent, unrevoked row is live."""
    now = utcnow()
    row = ProjectInvite(
        role="viewer", max_uses=1, uses=0, expires_at=now - dt.timedelta(seconds=1), revoked_at=None
    )
    assert row.state(now) == "expired"
    row.expires_at = now + dt.timedelta(days=1)
    assert row.state(now) == "live"
    row.uses = 1
    assert row.state(now) == "used_up"
    row.revoked_at = now
    assert row.state(now) == "revoked"


async def test_for_project_and_get_in_project_see_only_the_bound_workspace() -> None:
    ada, workspace, project = await _project_and_owner()
    zed, other, elsewhere = await _project_and_owner()
    async with session_scope(workspace_id=workspace.id) as s:
        first, _ = await ProjectInvite.mint(
            s, project_id=project.id, role="viewer", expires_in_days=7, created_by=ada.id
        )
        await s.commit()
    async with session_scope(
        workspace_id=workspace.id
    ) as s:  # its own transaction: created_at is now() at BEGIN
        second, _ = await ProjectInvite.mint(
            s, project_id=project.id, role="editor", expires_in_days=7, created_by=ada.id
        )
        await s.commit()
    async with session_scope(workspace_id=other.id) as s:
        theirs, _ = await ProjectInvite.mint(
            s, project_id=elsewhere.id, role="viewer", expires_in_days=7, created_by=zed.id
        )
        await s.commit()

    async with session_scope(workspace_id=workspace.id) as s:
        assert [r.id for r in await ProjectInvite.for_project(s, project.id)] == [
            second.id,
            first.id,
        ]  # newest first
        assert [r.id for r in await ProjectInvite.for_project(s, project.id, limit=1)] == [second.id]
        assert (await ProjectInvite.get_in_project(s, project.id, first.id)) is not None
        # another workspace's rows are invisible from here, by project and by id
        assert await ProjectInvite.for_project(s, elsewhere.id) == []
        assert await ProjectInvite.get_in_project(s, elsewhere.id, theirs.id) is None
        assert await ProjectInvite.get_in_project(s, project.id, theirs.id) is None  # wrong project too
    async with session_scope(workspace_id=other.id) as s:
        assert await ProjectInvite.get_in_project(s, project.id, first.id) is None
        assert [r.id for r in await ProjectInvite.for_project(s, elsewhere.id)] == [theirs.id]


async def test_take_use_under_another_workspaces_binding_spends_nothing() -> None:
    """Defense in depth: a wrong-workspace bind fails closed without spending a use."""
    ada, workspace, project = await _project_and_owner()
    _, other, _ = await _project_and_owner()
    async with session_scope(workspace_id=workspace.id) as s:
        row, _ = await ProjectInvite.mint(
            s, project_id=project.id, role="viewer", expires_in_days=7, created_by=ada.id
        )
        await s.commit()
    async with session_scope(workspace_id=other.id) as s:
        assert await ProjectInvite.take_use(s, row.id) is False
        await s.commit()
    async with session_scope(workspace_id=workspace.id) as s:
        again = await s.get(ProjectInvite, row.id)
        assert again is not None and again.uses == 0
        assert await ProjectInvite.take_use(s, row.id) is True
        await s.commit()
        await s.refresh(again)
        assert again.uses == 1


async def test_grant_raises_only_when_asked_and_keeps_the_first_adder() -> None:
    """A link's grant (`raise_only`) never lowers a role; the owner's set moves it either way;
    `added_by` is the first grant's, never rewritten."""
    ada, workspace, project = await _project_and_owner()
    async with session_scope(workspace_id=workspace.id) as s:
        bob, _ = await member(s, "Bob", workspace=workspace)
        cy, _ = await member(s, "Cy", workspace=workspace)
        await s.commit()

    async def grant(role: str, added_by: uuid.UUID, *, raise_only: bool) -> tuple[str, uuid.UUID]:
        async with session_scope(workspace_id=workspace.id) as s:
            row = await ProjectMember.grant(
                s, project.id, bob.id, role, added_by=added_by, raise_only=raise_only
            )
            await s.commit()
            return row.role, row.added_by

    assert await grant("viewer", ada.id, raise_only=True) == ("viewer", ada.id)  # an insert
    assert await grant("editor", cy.id, raise_only=True) == ("editor", ada.id)  # raised, adder kept
    assert await grant("viewer", cy.id, raise_only=True) == ("editor", ada.id)  # never lowered
    assert await grant("viewer", cy.id, raise_only=False) == ("viewer", ada.id)  # the owner's set
