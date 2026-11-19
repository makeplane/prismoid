# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The cap on team workspaces one person founds (ADR-086 section 2).

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import update

from prismoid.conf import settings
from prismoid.db.models.actor import Actor, Scope
from prismoid.db.models.workspace import Workspace, WorkspaceStatus
from prismoid.db.session import session_scope

pytestmark = pytest.mark.integration

_HINT = "Invite people into your workspace instead"


async def _signup(client: AsyncClient) -> dict[str, str]:
    body = {
        "email": f"cap{uuid.uuid4().hex[:12]}@example.com",
        "name": "Cap",
        "password": "a-long-enough-password",
    }
    r = await client.post("/v1/auth/signup", json=body)
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def _found(client: AsyncClient, h: dict[str, str]):
    return await client.post("/v1/workspaces", headers=h, json={"name": "Acme"})


async def test_the_cap_refuses_the_next_team_workspace_with_the_invite_hint(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.workspaces, "max_founded", 0)
    r = await _found(client, await _signup(client))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "workspace_cap"
    assert _HINT in r.json()["error"]["message"]


async def test_the_personal_workspace_does_not_count(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.workspaces, "max_founded", 1)
    h = await _signup(client)
    assert (await _found(client, h)).status_code == 201
    assert (await _found(client, h)).status_code == 403


async def test_a_suspended_workspace_still_counts(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.workspaces, "max_founded", 1)
    h = await _signup(client)
    r = await _found(client, h)
    assert r.status_code == 201
    async with session_scope() as s:
        await s.execute(
            update(Workspace)
            .where(Workspace.id == uuid.UUID(r.json()["id"]))
            .values(status=WorkspaceStatus.SUSPENDED.value)
        )
        await s.commit()
    assert (await _found(client, h)).status_code == 403


async def test_platform_admins_are_exempt(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.workspaces, "max_founded", 0)
    h = await _signup(client)
    me = (await client.get("/v1/auth/me", headers=h)).json()
    async with session_scope() as s:
        admin = await s.get(Actor, uuid.UUID(me["id"]))
        assert admin is not None
        admin.scopes = [*admin.scopes, Scope.ADMIN.value]
        await s.commit()
    assert (await _found(client, h)).status_code == 201
    assert (await client.get("/v1/auth/me", headers=h)).json()["can_create_workspace"] is True


async def test_unset_means_no_limit(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.workspaces, "max_founded", None)
    h = await _signup(client)
    for _ in range(3):
        assert (await _found(client, h)).status_code == 201


async def test_me_says_whether_you_can_create(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    h = await _signup(client)
    monkeypatch.setattr(settings.workspaces, "max_founded", 0)
    assert (await client.get("/v1/auth/me", headers=h)).json()["can_create_workspace"] is False
    monkeypatch.setattr(settings.workspaces, "max_founded", 1)
    assert (await client.get("/v1/auth/me", headers=h)).json()["can_create_workspace"] is True
    assert (await _found(client, h)).status_code == 201
    assert (await client.get("/v1/auth/me", headers=h)).json()["can_create_workspace"] is False
