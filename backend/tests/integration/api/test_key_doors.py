# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""An API key acts in its workspace for a person; the person's own acts need a login session.

A key can be leaked from a script or a CI log. Minting, listing or revoking keys, editing or
verifying the account, logging out, and listing, founding or joining workspaces reach past the one
workspace the key is pinned to, or would let a leaked key make itself permanent, so each answers a
key 403 and a login session as before. The key still reads `/v1/auth/me` and works in its workspace.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _session_and_key(client: AsyncClient) -> tuple[str, str, str]:
    email = f"key{uuid.uuid4().hex[:12]}@example.com"
    r = await client.post(
        "/v1/auth/signup", json={"email": email, "name": "Kay", "password": "a-long-enough-password"}
    )
    assert r.status_code == 201, r.text
    session = str(r.json()["token"])
    minted = await client.post("/v1/auth/api-keys", json={"name": "ci"}, headers=_h(session))
    assert minted.status_code == 201, minted.text
    return session, str(minted.json()["key"]), str(minted.json()["id"])


async def test_a_key_cannot_do_the_persons_own_acts(client: AsyncClient) -> None:
    session, key, key_id = await _session_and_key(client)
    refused = [
        await client.post("/v1/auth/api-keys", json={"name": "successor"}, headers=_h(key)),
        await client.get("/v1/auth/api-keys", headers=_h(key)),
        await client.delete(f"/v1/auth/api-keys/{key_id}", headers=_h(key)),
        await client.patch("/v1/auth/me", json={"name": "Renamed"}, headers=_h(key)),
        await client.post("/v1/auth/me/verify-email", json={"code": "ev_" + "x" * 30}, headers=_h(key)),
        await client.post("/v1/auth/me/verification", headers=_h(key)),
        await client.post("/v1/auth/logout", headers=_h(key)),
        await client.get("/v1/workspaces", headers=_h(key)),
        await client.post("/v1/workspaces", json={"name": "Elsewhere"}, headers=_h(key)),
    ]
    assert [r.status_code for r in refused] == [403] * len(refused), [r.text for r in refused]
    assert (await client.get("/v1/auth/me", headers=_h(key))).status_code == 200, (
        "a key still reads who it is"
    )
    assert (await client.get("/v1/auth/api-keys", headers=_h(session))).status_code == 200, (
        "a session still does"
    )
