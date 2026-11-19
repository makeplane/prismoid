# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Proving an address (ADR-086 section 3): signup works unverified; the emailed `ev_` code proves it,
and `allowance.address_proved` (where a distribution grants for a proof, ADR-103) hears each person's
proof exactly once - by the code, or at once by an invite to the address."""

from __future__ import annotations

import re
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.models.email_verification import EmailVerification
from prismoid.db.models.user import User
from prismoid.db.session import session_scope
from prismoid.operations import allowance
from prismoid.operations import auth as ops
from prismoid.schemas.ids import EMAIL_VERIFICATION_PREFIX, WORKSPACE_INVITE_PREFIX
from tests.support.cast import verify_email

pytestmark = pytest.mark.integration

_PASSWORD = "a-long-enough-password"


class Person:
    def __init__(self, token: str, email: str, me: dict[str, object]) -> None:
        self.token, self.email, self.me = token, email, me

    @property
    def h(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    @property
    def actor_id(self) -> uuid.UUID:
        return uuid.UUID(str(self.me["id"]))

    @property
    def workspace_id(self) -> uuid.UUID:
        workspace = self.me["workspace"]
        assert isinstance(workspace, dict)
        return uuid.UUID(str(workspace["id"]))


async def _signup(
    client: AsyncClient, name: str, *, invite_code: str | None = None, email: str | None = None
) -> Person:
    email = email or f"ev{uuid.uuid4().hex[:12]}@example.com"
    body: dict[str, object] = {"email": email, "name": name, "password": _PASSWORD}
    if invite_code:
        body["invite_code"] = invite_code
    r = await client.post("/v1/auth/signup", json=body)
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    me = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    return Person(token, email, me.json())


async def _code(capsys: pytest.CaptureFixture[str]) -> str:
    await ops.flush_outbound_email()
    out = capsys.readouterr().out
    found = re.findall(rf"{EMAIL_VERIFICATION_PREFIX}[A-Za-z0-9_\-]+", out)
    assert found, f"no verification code printed on the console:\n{out}"
    return found[-1]


@pytest.fixture
def heard(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """Whose proof `allowance.address_proved` heard, in order."""
    calls: list[uuid.UUID] = []

    async def address_proved(session: AsyncSession, user: User) -> None:
        del session
        calls.append(user.actor_id)

    monkeypatch.setattr(allowance, "address_proved", address_proved)
    return calls


async def _verify(client: AsyncClient, who: Person, code: str):
    return await client.post("/v1/auth/me/verify-email", headers=who.h, json={"code": code})


async def test_signup_proves_nothing_until_the_email_is_verified(
    client: AsyncClient, capsys: pytest.CaptureFixture[str], heard: list[uuid.UUID]
) -> None:
    ann = await _signup(client, "Ann")
    assert ann.me["email_verified"] is False
    assert (await _code(capsys)).startswith(EMAIL_VERIFICATION_PREFIX)
    assert heard == []


async def test_verifying_proves_the_address_once(
    client: AsyncClient, capsys: pytest.CaptureFixture[str], heard: list[uuid.UUID]
) -> None:
    ann = await _signup(client, "Ann")
    code = await _code(capsys)
    r = await _verify(client, ann, code)
    assert r.status_code == 200, r.text
    assert r.json()["email_verified"] is True
    assert heard == [ann.actor_id]

    again = await _verify(client, ann, code)  # a replay of the same link
    assert again.status_code == 200, again.text
    async with session_scope() as s:  # a fresh code for a verified person: no road to a second proof
        row, fresh = EmailVerification.issue(ann.actor_id)
        s.add(row)
        await s.commit()
    assert (await _verify(client, ann, fresh)).status_code == 200
    assert heard == [ann.actor_id]


async def test_a_code_is_only_for_its_own_user(
    client: AsyncClient, capsys: pytest.CaptureFixture[str], heard: list[uuid.UUID]
) -> None:
    ann = await _signup(client, "Ann")
    ann_code = await _code(capsys)
    ben = await _signup(client, "Ben")
    r = await _verify(client, ben, ann_code)
    assert r.status_code == 422
    assert r.json()["error"]["fields"]["code"] == "That link has expired or was already used."
    assert heard == []
    assert (await _verify(client, ann, ann_code)).status_code == 200  # Ben's try did not burn it
    assert heard == [ann.actor_id]


async def test_a_bad_code_is_a_422_and_proves_nothing(client: AsyncClient, heard: list[uuid.UUID]) -> None:
    ann = await _signup(client, "Ann")
    assert (await _verify(client, ann, f"{EMAIL_VERIFICATION_PREFIX}" + "x" * 43)).status_code == 422
    assert heard == []


async def test_an_invited_signup_is_proved_at_once(
    client: AsyncClient, capsys: pytest.CaptureFixture[str], heard: list[uuid.UUID]
) -> None:
    ada = await _signup(client, "Ada")
    await verify_email(str(ada.me["id"]))  # only a proven address sends invites
    team = await client.post("/v1/workspaces", headers=ada.h, json={"name": "Acme"})
    bob_email = f"bob{uuid.uuid4().hex[:8]}@example.com"
    capsys.readouterr()
    r = await client.post(
        f"/v1/workspaces/{team.json()['id']}/invites",
        headers=ada.h,
        json={"email": bob_email, "role": "member"},
    )
    assert r.status_code == 201, r.text
    await ops.flush_outbound_email()
    invite = re.findall(rf"{WORKSPACE_INVITE_PREFIX}[A-Za-z0-9_\-]+", capsys.readouterr().out)[-1]
    bob = await _signup(client, "Bob", invite_code=invite, email=bob_email)
    assert bob.me["email_verified"] is True  # the invite went to this address: it is proved
    assert heard.count(bob.actor_id) == 1
    await ops.flush_outbound_email()
    assert EMAIL_VERIFICATION_PREFIX not in capsys.readouterr().out  # and no code was mailed


async def test_a_failed_send_unmints_the_code(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def dead_relay(*, to: str, subject: str, text: str) -> bool:
        return False

    monkeypatch.setattr(ops.mailer, "send", dead_relay)
    ann = await _signup(client, "Ann")
    await ops.flush_outbound_email()
    live = await db.scalar(
        text("select count(*) from email_verifications where user_id = :u"), {"u": ann.actor_id}
    )
    assert live == 0


async def test_resend_supersedes_the_old_code(
    client: AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    ann = await _signup(client, "Ann")
    old = await _code(capsys)
    r = await client.post("/v1/auth/me/verification", headers=ann.h)
    assert r.status_code == 202, r.text
    new = await _code(capsys)
    assert new != old
    assert (await _verify(client, ann, old)).status_code == 422
    assert (await _verify(client, ann, new)).status_code == 200


async def test_resend_is_capped_per_hour(client: AsyncClient, capsys: pytest.CaptureFixture[str]) -> None:
    ann = await _signup(client, "Ann")  # the signup mail is the first of the hour's allowance
    cap = settings.auth.email_verification_max_per_hour
    for _ in range(cap - 1):
        assert (await client.post("/v1/auth/me/verification", headers=ann.h)).status_code == 202
    capsys.readouterr()
    assert (await client.post("/v1/auth/me/verification", headers=ann.h)).status_code == 429
    await ops.flush_outbound_email()
    assert EMAIL_VERIFICATION_PREFIX not in capsys.readouterr().out


async def test_the_mail_links_the_fragment_when_the_web_address_is_set(
    client: AsyncClient, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.api, "web_base_url", "https://app.example.test/")
    await _signup(client, "Ann")
    await ops.flush_outbound_email()
    out = capsys.readouterr().out
    assert "Verify your email for Prismoid" in out
    assert re.search(
        rf"https://app\.example\.test/verify-email#{EMAIL_VERIFICATION_PREFIX}[A-Za-z0-9_\-]+", out
    )
    assert "verify-email?" not in out


async def test_a_verified_person_is_not_mailed_again(
    client: AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    ann = await _signup(client, "Ann")
    assert (await _verify(client, ann, await _code(capsys))).status_code == 200
    assert (await client.post("/v1/auth/me/verification", headers=ann.h)).status_code == 202
    await ops.flush_outbound_email()
    assert EMAIL_VERIFICATION_PREFIX not in capsys.readouterr().out
