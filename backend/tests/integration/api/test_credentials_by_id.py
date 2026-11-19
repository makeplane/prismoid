# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Credentials by id: the screen socket's proof holds the credential
ROW its ticket named, never the token, so `live_principal` is `resolve`'s twin by id - the same
query, the same liveness and membership checks, the same principal - plus the account's
standing. A revoked, expired, removed or suspended credential stops proving, and a proof never
writes (no rebind, no use bump). The `credential` dependency names the row a request used."""

from __future__ import annotations

import datetime as dt
import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.api.deps import CredentialDep
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor, ActorStatus
from prismoid.db.models.api_key import ApiKey
from prismoid.db.models.session import AuthSession
from prismoid.db.models.user import User
from prismoid.db.models.workspace import Workspace, WorkspaceMember, WorkspaceRole, WorkspaceStatus
from prismoid.db.session import session_scope
from tests.support.cast import a_workspace, member

pytestmark = pytest.mark.integration


async def person(s: AsyncSession, name: str = "Ada") -> tuple[Actor, Workspace]:
    """A human with a users row (a login session hangs off it). In an unbound session they found
    a workspace of their own (`member`)."""
    actor, workspace = await member(s, name)
    s.add(User(actor_id=actor.id, email=f"{name.lower()}{uuid.uuid4().hex[:10]}@example.com", name=name))
    await s.flush()
    return actor, workspace


async def login(s: AsyncSession, actor: Actor, workspace: Workspace) -> tuple[AuthSession, str]:
    row, raw = AuthSession.issue(actor.id)
    row.active_workspace_id = workspace.id
    s.add(row)
    await s.flush()
    return row, raw


async def api_key(s: AsyncSession, actor: Actor, workspace: Workspace) -> tuple[ApiKey, str]:
    row, raw = ApiKey.issue(actor.id, "screen", workspace_id=workspace.id)
    s.add(row)
    await s.flush()
    return row, raw


# ── sessions ──────────────────────────────────────────────────────────────────────────────


async def test_a_live_session_proves_by_id_exactly_as_its_token_resolves() -> None:
    async with session_scope() as s:
        ada, workspace = await person(s)
        row, raw = await login(s, ada, workspace)
        await s.commit()
    async with session_scope() as s:  # the asserts stay inside: the scope's rollback expires its rows
        by_token = await AuthSession.resolve(s, raw)
        by_id = await AuthSession.live_principal(s, row.id)
        assert by_token is not None and by_id is not None
        assert (by_id.actor.id, by_id.workspace_id, by_id.role) == (ada.id, workspace.id, "owner")
        assert (by_id.auth_session_id, by_id.api_key_id) == (row.id, None)
        assert (by_token.actor.id, by_token.workspace_id, by_token.role, by_token.auth_session_id) == (
            by_id.actor.id,
            by_id.workspace_id,
            by_id.role,
            by_id.auth_session_id,
        )


async def test_a_revoked_expired_or_unknown_session_does_not_prove() -> None:
    async with session_scope() as s:
        ada, workspace = await person(s)
        revoked, revoked_raw = await login(s, ada, workspace)
        expired, _ = await login(s, ada, workspace)
        expired.expires_at = utcnow() - dt.timedelta(seconds=1)
        await s.commit()
        assert await AuthSession.revoke(s, revoked_raw) is True
        await s.commit()
    async with session_scope() as s:
        assert await AuthSession.live_principal(s, revoked.id) is None
        assert await AuthSession.live_principal(s, expired.id) is None
        assert await AuthSession.live_principal(s, uuid.uuid4()) is None


async def test_a_session_whose_membership_is_gone_does_not_prove_and_a_proof_never_rebinds() -> None:
    async with session_scope() as s:
        ada, workspace = await person(s)
        _, other = await a_workspace(s, "Other")
        await WorkspaceMember.add(
            s, other.id, ada.id, WorkspaceRole.MEMBER
        )  # somewhere resolve could rebind to
        row, raw = await login(s, ada, workspace)
        await WorkspaceMember.remove(s, workspace.id, ada.id)
        await s.commit()
    async with session_scope() as s:
        assert await AuthSession.live_principal(s, row.id) is None
        unchanged = await s.get(AuthSession, row.id)
        assert (
            unchanged is not None and unchanged.active_workspace_id == workspace.id
        )  # the proof wrote nothing
    async with session_scope() as s:  # the token's door, by contrast, rebinds
        rebound = await AuthSession.resolve(s, raw)
        assert rebound is not None and rebound.workspace_id == other.id


async def test_a_suspended_person_or_workspace_does_not_prove() -> None:
    async with session_scope() as s:
        ada, ada_workspace = await person(s, "Ada")
        ada_session, _ = await login(s, ada, ada_workspace)
        ada_key, _ = await api_key(s, ada, ada_workspace)
        await s.commit()
    async with session_scope() as s:  # a fresh, unbound unit of work: Bo founds his own workspace
        bo, bo_workspace = await person(s, "Bo")
        bo_session, _ = await login(s, bo, bo_workspace)
        bo_key, _ = await api_key(s, bo, bo_workspace)
        await s.commit()
    async with session_scope() as s:
        await s.execute(update(Actor).where(Actor.id == ada.id).values(status=ActorStatus.SUSPENDED.value))
        await s.execute(
            update(Workspace)
            .where(Workspace.id == bo_workspace.id)
            .values(status=WorkspaceStatus.SUSPENDED.value)
        )
        await s.commit()
    async with session_scope() as s:
        assert await AuthSession.live_principal(s, ada_session.id) is None
        assert await ApiKey.live_principal(s, ada_key.id) is None
        assert await AuthSession.live_principal(s, bo_session.id) is None
        assert await ApiKey.live_principal(s, bo_key.id) is None


# ── API keys ──────────────────────────────────────────────────────────────────────────────


async def test_an_api_key_proves_by_id_and_resolve_names_the_key() -> None:
    async with session_scope() as s:
        ada, workspace = await person(s)
        key, raw = await api_key(s, ada, workspace)
        await s.commit()
    async with session_scope() as s:
        by_token = await ApiKey.resolve(s, raw)
        by_id = await ApiKey.live_principal(s, key.id)
        assert by_token is not None and by_id is not None
        assert (by_token.api_key_id, by_token.auth_session_id) == (key.id, None)
        assert (by_id.actor.id, by_id.workspace_id, by_id.role, by_id.api_key_id) == (
            ada.id,
            workspace.id,
            "owner",
            key.id,
        )


async def test_a_proof_is_not_a_use_and_a_revoked_key_stops_proving() -> None:
    async with session_scope() as s:
        ada, workspace = await person(s)
        key, _ = await api_key(s, ada, workspace)
        await s.commit()
    async with session_scope() as s:
        assert await ApiKey.live_principal(s, key.id) is not None
        fresh = await s.get(ApiKey, key.id)
        assert fresh is not None and fresh.last_used_at is None  # a re-proof never bumps it
    async with session_scope(workspace_id=workspace.id) as s:
        assert await ApiKey.revoke(s, ada.id, key.id) is True
        await s.commit()
    async with session_scope() as s:
        assert await ApiKey.live_principal(s, key.id) is None


# ── the credential a request used ─────────────────────────────────────────────────────────


async def test_the_credential_dependency_names_the_row_a_request_authenticated_with() -> None:
    async def who(cred: CredentialDep) -> dict[str, str]:  # a test-only route, never in api/urls.py
        return {"id": str(cred.id), "kind": cred.kind}

    probe = FastAPI()
    probe.add_api_route("/probe", who, methods=["GET"])

    async with session_scope() as s:
        ada, workspace = await person(s)
        row, cs = await login(s, ada, workspace)
        key, ck = await api_key(s, ada, workspace)
        await s.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=probe), base_url="http://test") as client:
        by_session = (await client.get("/probe", headers={"Authorization": f"Bearer {cs}"})).json()
        by_key = (await client.get("/probe", headers={"Authorization": f"Bearer {ck}"})).json()
    assert by_session == {"id": str(row.id), "kind": "session"}
    assert by_key == {"id": str(key.id), "kind": "api_key"}
