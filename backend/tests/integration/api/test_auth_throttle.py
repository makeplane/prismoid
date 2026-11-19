# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The auth doors' durable buckets: counted in Postgres per account AND per client address, with a
growing backoff after consecutive failed logins, answered over HTTP as 429 with Retry-After.

Real rows on the test database, so the counts are the ones every replica would see. Each test
uses fresh addresses and client addresses; conftest deletes the buckets each test made after it.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.api.app import create_app
from prismoid.conf import settings
from prismoid.db.models.auth_throttle import AuthThrottle
from prismoid.db.models.password_reset import PasswordReset
from prismoid.db.models.user import User
from prismoid.db.session import session_scope
from prismoid.errors import Forbidden, RateLimited, Unauthorized
from prismoid.operations import auth as ops
from prismoid.operations import throttle
from prismoid.schemas.ids import hash_token

pytestmark = pytest.mark.integration

_PASSWORD = "a-long-enough-password"


def an_email() -> str:
    return f"throttle-{uuid.uuid4().hex[:12]}@example.com"


def account_bucket(email: str, ip: str) -> str:
    """The login door's account bucket, as the door names it: the account seen from one client
    address, both hashed."""
    return f"login:acct:{hash_token(User.normalize_email(email))}:ip:{hash_token(ip)}"


def an_ip() -> str:
    return f"198.51.100.{uuid.uuid4().int % 250 + 1}"


OWNER_IP = "203.0.113.7"  # outside an_ip()'s range: the owner's address is never the guesser's


async def _signed_up(db: AsyncSession) -> str:
    email = an_email()
    await ops.signup(db, email=email, name="Thea", password=_PASSWORD, ip=an_ip())
    return email


async def test_one_account_is_capped_from_one_client_address(db: AsyncSession) -> None:
    """One address gets a line per account; another address is counted on its own, so a burst
    from one never refuses the owner at another."""
    email = await _signed_up(db)
    limit = settings.auth.login_rate_limit_per_minute
    ip = an_ip()
    for _ in range(limit):
        await ops.login(db, email=email, password=_PASSWORD, ip=ip)
    with pytest.raises(RateLimited) as caught:
        await ops.login(db, email=email, password=_PASSWORD, ip=ip)
    assert 0 < caught.value.retry_after <= 60
    assert (await ops.login(db, email=email, password=_PASSWORD, ip=OWNER_IP)).user.email == email


async def test_a_stranger_cannot_lock_the_owner_out(db: AsyncSession) -> None:
    """Wrong guesses from one address earn that address the backoff; the owner, at another
    address, still logs in with the right password while the stranger keeps trying."""
    email = await _signed_up(db)
    stranger = an_ip()
    for _ in range(settings.auth.login_backoff_after_failures):
        with pytest.raises(Unauthorized):
            await ops.login(db, email=email, password="wrong-password", ip=stranger)
    with pytest.raises(RateLimited):
        await ops.login(db, email=email, password="wrong-password", ip=stranger)
    with pytest.raises(RateLimited):  # even the right password, from the guesser's address
        await ops.login(db, email=email, password=_PASSWORD, ip=stranger)
    assert (await ops.login(db, email=email, password=_PASSWORD, ip=OWNER_IP)).user.email == email
    with pytest.raises(RateLimited):  # and the owner's login lifted nothing for the stranger
        await ops.login(db, email=email, password="wrong-password", ip=stranger)


async def test_a_password_reset_ends_the_backoff(db: AsyncSession) -> None:
    email = await _signed_up(db)
    ip = an_ip()
    for _ in range(settings.auth.login_backoff_after_failures):
        with pytest.raises(Unauthorized):
            await ops.login(db, email=email, password="wrong-password", ip=ip)
    with pytest.raises(RateLimited):
        await ops.login(db, email=email, password=_PASSWORD, ip=ip)

    async with session_scope(platform=True) as session:
        user = await User.by_email(session, email)
        assert user is not None
        row, code = PasswordReset.issue(user.actor_id)
        session.add(row)
        await session.commit()
    await ops.reset_password(db, code=code, new_password="the-brand-new-password", ip=OWNER_IP)
    await ops.flush_outbound_email()
    async with session_scope(platform=True) as session:
        assert await session.get(AuthThrottle, account_bucket(email, ip)) is None

    assert (await ops.login(db, email=email, password="the-brand-new-password", ip=ip)).user.email == email


async def test_one_client_address_is_capped_across_accounts(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spraying one password over many accounts from one address meets the address's bucket."""
    monkeypatch.setattr(settings.auth, "login_ip_rate_limit_per_minute", 3)
    monkeypatch.setattr(settings.auth, "login_ip_backoff_after_failures", 100)
    ip = an_ip()
    for _ in range(3):
        with pytest.raises(Unauthorized):
            await ops.login(db, email=an_email(), password="wrong-password", ip=ip)
    with pytest.raises(RateLimited):
        await ops.login(db, email=an_email(), password="wrong-password", ip=ip)
    with pytest.raises(Unauthorized):  # another address is its own bucket
        await ops.login(db, email=an_email(), password="wrong-password", ip=an_ip())


async def test_consecutive_failures_earn_a_backoff_that_doubles(db: AsyncSession) -> None:
    email = await _signed_up(db)
    after = settings.auth.login_backoff_after_failures
    base = settings.auth.login_backoff_base_seconds
    ip = an_ip()
    for _ in range(after):
        with pytest.raises(Unauthorized):
            await ops.login(db, email=email, password="wrong-password", ip=ip)
    # The right password no longer gets in from this address while the backoff runs.
    with pytest.raises(RateLimited) as caught:
        await ops.login(db, email=email, password=_PASSWORD, ip=ip)
    assert base - 2 <= caught.value.retry_after <= base

    # Let the block lapse, fail once more: the next block is twice as long.
    bucket = account_bucket(email, ip)
    await _lapse_block(bucket)
    with pytest.raises(Unauthorized):
        await ops.login(db, email=email, password="wrong-password", ip=ip)
    with pytest.raises(RateLimited) as caught:
        await ops.login(db, email=email, password=_PASSWORD, ip=ip)
    assert 2 * base - 2 <= caught.value.retry_after <= 2 * base


async def test_the_backoff_is_capped(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.auth, "login_backoff_after_failures", 1)
    monkeypatch.setattr(settings.auth, "login_backoff_max_seconds", 45)
    bucket = throttle.Bucket(f"test:acct:{uuid.uuid4().hex}", per_minute=1000, backoff_after=1)
    for _ in range(6):
        await throttle.take(bucket, message="x")
        await throttle.failed(bucket)
        await _lapse_block(bucket.key)
    await throttle.take(bucket, message="x")
    await throttle.failed(bucket)
    with pytest.raises(RateLimited) as caught:
        await throttle.take(bucket, message="x")
    assert caught.value.retry_after <= 45


async def test_a_huge_failure_run_still_blocks_at_the_cap(db: AsyncSession) -> None:
    """2^failures overflows Postgres' float8 long before a run this long; the exponent is capped,
    so the failure still counts and the block is still the cap."""
    bucket = throttle.Bucket(f"test:acct:{uuid.uuid4().hex}", per_minute=1000, backoff_after=5)
    await throttle.take(bucket, message="x")
    async with session_scope(platform=True) as session:
        await session.execute(
            update(AuthThrottle)
            .where(AuthThrottle.bucket == bucket.key)
            .values(failures=1_000_000, failed_at=func.now())
        )
        await session.commit()
    await throttle.failed(bucket)
    with pytest.raises(RateLimited) as caught:
        await throttle.take(bucket, message="x")
    assert caught.value.retry_after <= settings.auth.login_backoff_max_seconds
    async with session_scope(platform=True) as session:
        row = await session.get(AuthThrottle, bucket.key)
        assert row is not None and row.failures == 1_000_001


async def test_a_success_ends_the_failure_run(db: AsyncSession) -> None:
    email = await _signed_up(db)
    after = settings.auth.login_backoff_after_failures
    ip = an_ip()
    for _ in range(after - 1):
        with pytest.raises(Unauthorized):
            await ops.login(db, email=email, password="wrong-password", ip=ip)
    await ops.login(db, email=email, password=_PASSWORD, ip=ip)
    for _ in range(after - 1):  # a fresh run: still short of the backoff
        with pytest.raises(Unauthorized):
            await ops.login(db, email=email, password="wrong-password", ip=ip)
    assert (await ops.login(db, email=email, password=_PASSWORD, ip=ip)).user.email == email


async def test_an_unknown_account_backs_off_like_a_known_one(db: AsyncSession) -> None:
    """The backoff must not tell an attacker which addresses have accounts."""
    email, ip = an_email(), an_ip()
    for _ in range(settings.auth.login_backoff_after_failures):
        with pytest.raises(Unauthorized):
            await ops.login(db, email=email, password="wrong-password", ip=ip)
    with pytest.raises(RateLimited):
        await ops.login(db, email=email, password="wrong-password", ip=ip)


async def test_the_count_survives_the_process(db: AsyncSession) -> None:
    """The buckets are rows, not memory: what one replica counted, another sees."""
    email, ip = an_email(), an_ip()
    for _ in range(2):
        with pytest.raises(Unauthorized):
            await ops.login(db, email=email, password="wrong-password", ip=ip)
    bucket = account_bucket(email, ip)
    async with session_scope(platform=True) as session:
        row = await session.get(AuthThrottle, bucket)
        assert row is not None and row.hits == 2 and row.failures == 2


async def test_the_table_holds_no_email(db: AsyncSession) -> None:
    email = an_email()
    with pytest.raises(Unauthorized):
        await ops.login(db, email=email, password="wrong-password", ip=an_ip())
    async with session_scope(platform=True) as session:
        found = await session.scalar(
            text("select count(*) from auth_throttles where bucket like :e"), {"e": f"%{email}%"}
        )
    assert found == 0


async def test_a_signup_burst_on_one_address_is_capped(db: AsyncSession) -> None:
    """Every attempt counts, the refused ones too: one address cannot be retried at will from
    rotating client addresses."""
    email = await _signed_up(db)
    for _ in range(settings.auth.signup_rate_limit_per_minute - 1):
        with pytest.raises(Forbidden):
            await ops.signup(db, email=email, name="Same", password=_PASSWORD, ip=an_ip())
    with pytest.raises(RateLimited):
        await ops.signup(db, email=email, name="Same", password=_PASSWORD, ip=an_ip())


async def test_idle_buckets_are_swept_and_live_ones_kept(db: AsyncSession) -> None:
    idle = throttle.Bucket(f"test:ip:{uuid.uuid4().hex}", per_minute=10)
    live = throttle.Bucket(f"test:ip:{uuid.uuid4().hex}", per_minute=10)
    await throttle.take(idle, live, message="x")
    async with session_scope(platform=True) as session:
        await session.execute(
            update(AuthThrottle)
            .where(AuthThrottle.bucket == idle.key)
            .values(window_start=AuthThrottle.window_start - dt.timedelta(days=2))
        )
        await session.commit()
    async with session_scope(platform=True) as session:
        assert await throttle.purge_stale(session) >= 1
        assert await session.get(AuthThrottle, idle.key) is None
        assert await session.get(AuthThrottle, live.key) is not None


async def test_the_login_door_answers_429_with_retry_after() -> None:
    email = an_email()
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        for _ in range(settings.auth.login_rate_limit_per_minute):
            refused = await client.post("/v1/auth/login", json={"email": email, "password": "wrong-password"})
            assert refused.status_code in (401, 429)
        answered = await client.post("/v1/auth/login", json={"email": email, "password": "wrong-password"})
    assert answered.status_code == 429
    assert int(answered.headers["Retry-After"]) > 0
    assert answered.json()["error"]["code"] == "rate_limited"


async def _lapse_block(bucket: str) -> None:
    """Move a bucket's backoff and window into the past, as if the wait had been sat out."""
    async with session_scope(platform=True) as session:
        await session.execute(
            update(AuthThrottle)
            .where(AuthThrottle.bucket == bucket)
            .values(
                blocked_until=AuthThrottle.blocked_until - dt.timedelta(hours=1),
                window_start=AuthThrottle.window_start - dt.timedelta(minutes=2),
            )
        )
        await session.commit()
