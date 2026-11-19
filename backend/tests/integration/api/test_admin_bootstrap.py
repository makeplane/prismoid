# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The first platform admin is made on the box, never through the web: `prismoid admin bootstrap
--email X` grants an existing account the admin scope, or makes X's account with it and prints a
one-time set-password code (the password-reset `cr_` door: hashed at rest, TTL'd, single use).

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from prismoid.db.models.actor import Actor, Scope
from prismoid.db.models.user import User
from prismoid.db.session import session_scope
from prismoid.errors import Unauthorized
from prismoid.operations import admin_ops
from prismoid.operations import auth as auth_ops
from prismoid.schemas.ids import PASSWORD_RESET_PREFIX, hash_token

pytestmark = pytest.mark.integration

_PASSWORD = "a-long-enough-password"


@pytest.fixture(autouse=True)
async def drained_outbox() -> AsyncIterator[None]:
    yield
    await auth_ops.flush_outbound_email()  # the reset's courtesy notice, sent off the request path


def an_email() -> str:
    return f"boot{uuid.uuid4().hex[:12]}@example.com"


async def _is_admin(email: str) -> bool:
    async with session_scope() as s:
        user = await User.by_email(s, email)
        assert user is not None
        actor = await s.get(Actor, user.actor_id)
        return actor is not None and actor.has_scope(Scope.ADMIN)


async def test_a_new_address_gets_an_admin_account_and_a_one_time_code() -> None:
    email = an_email()
    async with session_scope() as s:
        result = await admin_ops.bootstrap_admin(s, email=email)
    assert result.created and result.code is not None
    assert result.code.startswith(PASSWORD_RESET_PREFIX)
    assert await _is_admin(email)

    async with session_scope() as s:  # hashed at rest: the plaintext is nowhere in the table
        stored = await s.scalar(
            text("select count(*) from password_resets where code_hash = :h"), {"h": hash_token(result.code)}
        )
        plain = await s.scalar(
            text("select count(*) from password_resets where code_hash = :c"), {"c": result.code}
        )
    assert (stored, plain) == (1, 0)

    async with session_scope() as s:  # nobody can log in before the code is spent
        with pytest.raises(Unauthorized):
            await auth_ops.login(s, email=email, password=_PASSWORD)
    async with session_scope() as s:
        await auth_ops.reset_password(s, code=result.code, new_password=_PASSWORD)
    async with session_scope() as s:
        login = await auth_ops.login(s, email=email, password=_PASSWORD)
    assert login.user.email == email
    async with session_scope() as s:  # single use
        with pytest.raises(Unauthorized):
            await auth_ops.reset_password(s, code=result.code, new_password=_PASSWORD + "x")


async def test_an_existing_account_is_granted_admin_idempotently() -> None:
    email = an_email()
    async with session_scope() as s:
        await auth_ops.signup(s, email=email, name="Ops", password=_PASSWORD)
    assert not await _is_admin(email)
    for _ in range(2):
        async with session_scope() as s:
            result = await admin_ops.bootstrap_admin(s, email=email)
        assert not result.created and result.code is None
        assert await _is_admin(email)
    async with session_scope() as s:  # the password is untouched
        await auth_ops.login(s, email=email, password=_PASSWORD)


async def test_a_new_code_replaces_an_expired_one_for_an_existing_account() -> None:
    email = an_email()
    async with session_scope() as s:
        first = await admin_ops.bootstrap_admin(s, email=email)
    async with session_scope() as s:
        again = await admin_ops.bootstrap_admin(s, email=email, new_code=True)
    assert first.code and again.code and first.code != again.code
    async with session_scope() as s:  # the older code is superseded
        with pytest.raises(Unauthorized):
            await auth_ops.reset_password(s, code=first.code, new_password=_PASSWORD)
    async with session_scope() as s:
        await auth_ops.reset_password(s, code=again.code, new_password=_PASSWORD)
