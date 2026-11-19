# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The device door (ADR-100): a phone registers for push with its login session in a workspace, a
token that comes back moves to whoever registers it now, an overlong token is a 422, a person removes
only their own phones, and a phone ends with the session that registered it - a logout, a revocation,
a password reset. A server without push keeps no phones: both routes answer 404.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import fcm
from prismoid.conf import settings
from prismoid.db.models.push_device import PushDevice
from prismoid.db.models.session import AuthSession
from prismoid.db.models.workspace import Workspace

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("push_on")]  # off, the routes answer 404


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _phone(token: str, platform: str = "ios") -> dict[str, str]:
    return {"token": token, "platform": platform, "app_id": "so.plane.prismoid", "app_version": "1.4.0"}


async def _signup(client: AsyncClient, name: str) -> str:
    signed = await client.post(
        "/v1/auth/signup",
        json={
            "email": f"dv{uuid.uuid4().hex[:12]}@example.com",
            "name": name,
            "password": "a-long-enough-password",
        },
    )
    assert signed.status_code == 201, signed.text
    return str(signed.json()["token"])


async def _device(db: AsyncSession, token: str) -> PushDevice | None:
    db.expire_all()
    return await db.scalar(select(PushDevice).where(PushDevice.token == token))


async def test_a_phone_registers_and_the_token_never_comes_back(
    client: AsyncClient, db: AsyncSession
) -> None:
    session = await _signup(client, "Ana")
    fcm_token = f"fcm-{uuid.uuid4().hex}"

    made = await client.post("/v1/push/devices", json=_phone(fcm_token), headers=_h(session))

    assert made.status_code == 201, made.text
    body = made.json()
    assert set(body) == {"id", "platform", "app_id", "app_version", "created_at", "last_seen_at"}
    assert (body["platform"], body["app_id"], body["app_version"]) == ("ios", "so.plane.prismoid", "1.4.0")
    row = await _device(db, fcm_token)
    assert row is not None and str(row.id) == body["id"]


async def test_a_known_token_moves_to_whoever_registers_it_now(client: AsyncClient, db: AsyncSession) -> None:
    ana, bo = await _signup(client, "Ana"), await _signup(client, "Bo")
    fcm_token = f"fcm-{uuid.uuid4().hex}"
    first = (await client.post("/v1/push/devices", json=_phone(fcm_token), headers=_h(ana))).json()
    ana_row = await _device(db, fcm_token)
    assert ana_row is not None
    ana_actor, ana_session = ana_row.actor_id, ana_row.session_id

    again = await client.post(
        "/v1/push/devices", json={**_phone(fcm_token, "android"), "app_version": "1.5.0"}, headers=_h(bo)
    )

    assert again.status_code == 201, again.text
    assert again.json()["id"] == first["id"]  # one row per token
    assert again.json()["created_at"] == first["created_at"]
    assert (again.json()["platform"], again.json()["app_version"]) == ("android", "1.5.0")
    moved = await _device(db, fcm_token)
    assert moved is not None
    assert moved.actor_id != ana_actor and moved.session_id != ana_session
    assert moved.last_seen_at >= ana_row.last_seen_at


async def test_an_api_key_registers_no_phone(client: AsyncClient) -> None:
    session = await _signup(client, "Ana")
    key = (await client.post("/v1/auth/api-keys", json={"name": "ci"}, headers=_h(session))).json()["key"]
    refused = await client.post("/v1/push/devices", json=_phone(f"fcm-{uuid.uuid4().hex}"), headers=_h(key))
    assert refused.status_code == 403, refused.text


async def test_a_body_outside_the_contract_is_refused(client: AsyncClient) -> None:
    session = await _signup(client, "Ana")
    for bad in (_phone("t", "windows"), {**_phone("t"), "token": ""}, {"token": "t", "platform": "ios"}):
        assert (await client.post("/v1/push/devices", json=bad, headers=_h(session))).status_code == 422


async def test_an_overlong_token_is_refused_before_the_database(client: AsyncClient) -> None:
    """A token past the cap, in bytes, would overflow the unique index's btree row: a 422, never a 500."""
    session = await _signup(client, "Ana")
    refused = await client.post("/v1/push/devices", json=_phone("t" * 4096), headers=_h(session))
    assert refused.status_code == 422, refused.text
    assert (
        await client.post("/v1/push/devices", json=_phone("t" * 1024), headers=_h(session))
    ).status_code == 201
    # the index counts bytes: 600 three-byte characters are under a character cap, over the byte one
    multibyte = await client.post("/v1/push/devices", json=_phone("€" * 600), headers=_h(session))
    assert multibyte.status_code == 422, multibyte.text


async def test_a_person_with_no_workspace_left_is_refused_and_logout_still_ends_the_phone(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The workspace door applies here as on every route but the way back in: with no workspace left
    there is no inbox to push, and logout - a way-back-in route - still ends the phone."""
    session = await _signup(client, "Ana")
    fcm_token = f"fcm-{uuid.uuid4().hex}"
    made = await client.post("/v1/push/devices", json=_phone(fcm_token), headers=_h(session))
    assert made.status_code == 201, made.text
    personal = (await client.get("/v1/auth/me", headers=_h(session))).json()["workspace"]["id"]
    await db.execute(update(Workspace).where(Workspace.id == uuid.UUID(personal)).values(status="suspended"))
    await db.commit()
    assert (await client.get("/v1/auth/me", headers=_h(session))).json()["workspace"] is None

    again = await client.post("/v1/push/devices", json=_phone(f"fcm-{uuid.uuid4().hex}"), headers=_h(session))
    assert again.status_code == 403, again.text
    removed = await client.delete(f"/v1/push/devices/{made.json()['id']}", headers=_h(session))
    assert removed.status_code == 403, removed.text

    assert (await client.post("/v1/auth/logout", headers=_h(session))).status_code in (200, 204)
    assert await _device(db, fcm_token) is None


async def test_a_person_removes_their_own_phone_and_nobody_elses(
    client: AsyncClient, db: AsyncSession
) -> None:
    ana, bo = await _signup(client, "Ana"), await _signup(client, "Bo")
    fcm_token = f"fcm-{uuid.uuid4().hex}"
    device_id = (await client.post("/v1/push/devices", json=_phone(fcm_token), headers=_h(ana))).json()["id"]

    theirs = await client.delete(f"/v1/push/devices/{device_id}", headers=_h(bo))
    assert theirs.status_code == 404, theirs.text
    assert await _device(db, fcm_token) is not None

    mine = await client.delete(f"/v1/push/devices/{device_id}", headers=_h(ana))
    assert mine.status_code == 204, mine.text
    assert await _device(db, fcm_token) is None
    assert (await client.delete(f"/v1/push/devices/{device_id}", headers=_h(ana))).status_code == 404


async def test_a_logout_takes_the_sessions_phones_and_only_them(
    client: AsyncClient, db: AsyncSession
) -> None:
    email = f"dv{uuid.uuid4().hex[:12]}@example.com"
    signed = await client.post(
        "/v1/auth/signup", json={"email": email, "name": "Ana", "password": "a-long-enough-password"}
    )
    phone_session = str(signed.json()["token"])
    laptop = await client.post("/v1/auth/login", json={"email": email, "password": "a-long-enough-password"})
    laptop_session = str(laptop.json()["token"])
    phone, tablet = f"fcm-{uuid.uuid4().hex}", f"fcm-{uuid.uuid4().hex}"
    await client.post("/v1/push/devices", json=_phone(phone), headers=_h(phone_session))
    await client.post("/v1/push/devices", json=_phone(tablet), headers=_h(laptop_session))

    assert (await client.post("/v1/auth/logout", headers=_h(phone_session))).status_code in (200, 204)

    assert await _device(db, phone) is None
    assert await _device(db, tablet) is not None


async def test_revoking_every_session_takes_every_phone(client: AsyncClient, db: AsyncSession) -> None:
    session = await _signup(client, "Ana")
    fcm_token = f"fcm-{uuid.uuid4().hex}"
    await client.post("/v1/push/devices", json=_phone(fcm_token), headers=_h(session))
    row = await _device(db, fcm_token)
    assert row is not None

    assert await AuthSession.revoke_all_for(db, row.actor_id) == 1
    await db.commit()

    assert await _device(db, fcm_token) is None


async def test_one_revoked_session_takes_its_phone(client: AsyncClient, db: AsyncSession) -> None:
    session = await _signup(client, "Ana")
    fcm_token = f"fcm-{uuid.uuid4().hex}"
    await client.post("/v1/push/devices", json=_phone(fcm_token), headers=_h(session))
    row = await _device(db, fcm_token)
    assert row is not None

    assert await AuthSession.revoke_one(db, row.actor_id, row.session_id)
    await db.commit()

    assert await _device(db, fcm_token) is None


async def test_a_purged_session_cascades_to_its_phone(client: AsyncClient, db: AsyncSession) -> None:
    session = await _signup(client, "Ana")
    fcm_token = f"fcm-{uuid.uuid4().hex}"
    await client.post("/v1/push/devices", json=_phone(fcm_token), headers=_h(session))
    row = await _device(db, fcm_token)
    assert row is not None

    auth_row = await db.get(AuthSession, row.session_id)
    assert auth_row is not None
    await db.delete(auth_row)
    await db.commit()

    assert await _device(db, fcm_token) is None


async def test_a_server_without_push_keeps_no_phones(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No push key, which is every self-hosted server: registering answers 404 as a missing route
    would and stores nothing, and a phone kept from when push was on can't be removed through a door
    that is closed (its session's end still removes it)."""
    session = await _signup(client, "Ana")
    kept = await client.post("/v1/push/devices", json=_phone("tok-kept"), headers=_h(session))
    assert kept.status_code == 201, kept.text

    fcm.install(None)
    monkeypatch.setattr(settings.push, "credentials_json", SecretStr(""))

    refused = await client.post("/v1/push/devices", json=_phone("tok-off"), headers=_h(session))
    assert refused.status_code == 404, refused.text
    assert await _device(db, "tok-off") is None
    gone = await client.delete(f"/v1/push/devices/{kept.json()['id']}", headers=_h(session))
    assert gone.status_code == 404, gone.text
