# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""An API key acts only in the workspace it was minted in, on every door - the doors that bind the
workspace a code names (a Project join link, a workspace invite) included. Where a key would join
another workspace, the answer is the stranger's 404 and nothing is granted; a login session, which
belongs to its person rather than to one workspace, still joins.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from prismoid.api.app import create_app
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.workspace import WorkspaceMember, WorkspaceRole
from prismoid.db.session import session_scope
from prismoid.operations import auth as auth_ops
from prismoid.schemas.ids import WORKSPACE_INVITE_PREFIX
from tests.support.cast import verify_email

pytestmark = pytest.mark.integration

_PASSWORD = "a-long-enough-password"


@pytest.fixture
async def client(silent_dispatcher: None) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        yield c
    await auth_ops.flush_outbound_email()


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _signup(client: AsyncClient, name: str) -> tuple[dict[str, str], str, str]:
    """A person's session headers, actor id and email; their personal workspace is the active one."""
    email = f"keypin{uuid.uuid4().hex[:12]}@example.com"
    r = await client.post("/v1/auth/signup", json={"email": email, "name": name, "password": _PASSWORD})
    assert r.status_code == 201, r.text
    headers = _h(r.json()["token"])
    me = await client.get("/v1/auth/me", headers=headers)
    await verify_email(me.json()["id"])  # an inviter's address is proven (ADR-086)
    return headers, str(me.json()["id"]), email


async def _team(client: AsyncClient, owner: dict[str, str]) -> str:
    r = await client.post("/v1/workspaces", headers=owner, json={"name": "Acme"})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _key(client: AsyncClient, person: dict[str, str]) -> dict[str, str]:
    """An API key minted in the person's active (personal) workspace."""
    r = await client.post("/v1/auth/api-keys", headers=person, json={"name": "ci"})
    assert r.status_code == 201, r.text
    return _h(r.json()["key"])


async def test_a_key_cannot_take_a_project_link_into_another_workspace(client: AsyncClient) -> None:
    ada, _, _ = await _signup(client, "Ada")
    acme = await _team(client, ada)
    switched = await client.put("/v1/auth/session/workspace", headers=ada, json={"workspace_id": acme})
    assert switched.status_code == 200, switched.text
    made = await client.post("/v1/projects", headers=ada, json={"name": "Launch"})
    assert made.status_code == 201, made.text
    project_id = made.json()["id"]
    link = await client.post(f"/v1/projects/{project_id}/invites", headers=ada, json={"role": "editor"})
    assert link.status_code == 201, link.text
    code = link.json()["code"]

    bob, bob_id, _ = await _signup(client, "Bob")
    async with session_scope() as s:  # Bob is in Acme, so only his credential decides
        await WorkspaceMember.add(s, uuid.UUID(acme), uuid.UUID(bob_id), WorkspaceRole.MEMBER)
        await s.commit()
    key = await _key(client, bob)

    refused = await client.post("/v1/projects/invites/accept", headers=key, json={"code": code})
    assert refused.status_code == 404, refused.text
    async with session_scope() as s:
        assert await ProjectMember.get(s, uuid.UUID(project_id), uuid.UUID(bob_id)) is None

    joined = await client.post("/v1/projects/invites/accept", headers=bob, json={"code": code})
    assert joined.status_code == 200, joined.text


async def test_a_key_cannot_accept_an_invite_into_another_workspace(
    client: AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    ada, _, _ = await _signup(client, "Ada")
    acme = await _team(client, ada)
    bob, bob_id, bob_email = await _signup(client, "Bob")
    key = await _key(client, bob)
    capsys.readouterr()
    invited = await client.post(
        f"/v1/workspaces/{acme}/invites", headers=ada, json={"email": bob_email, "role": "admin"}
    )
    assert invited.status_code == 201, invited.text
    await auth_ops.flush_outbound_email()
    code = re.findall(rf"{WORKSPACE_INVITE_PREFIX}[A-Za-z0-9_\-]+", capsys.readouterr().out)[-1]

    refused = await client.post("/v1/workspaces/invites/accept", headers=key, json={"code": code})
    assert refused.status_code == 403, refused.text  # a key never takes the way back in, code or not
    async with session_scope() as s:
        assert await WorkspaceMember.get(s, uuid.UUID(acme), uuid.UUID(bob_id)) is None

    joined = await client.post("/v1/workspaces/invites/accept", headers=bob, json={"code": code})
    assert joined.status_code == 200, joined.text


async def test_an_admins_key_stays_in_its_workspace_and_never_opens_the_console(client: AsyncClient) -> None:
    from prismoid.operations import admin_ops

    admin, _, email = await _signup(client, "Ada")
    async with session_scope() as s:
        await admin_ops.bootstrap_admin(s, email=email)  # an existing account gains the admin scope
    key = await _key(client, admin)

    assert (await client.get("/v1/admin/workspaces", headers=admin)).status_code == 200, "a login session"
    refused = await client.get("/v1/admin/workspaces", headers=key)
    assert refused.status_code == 403, refused.text
    assert "login session" in refused.text
