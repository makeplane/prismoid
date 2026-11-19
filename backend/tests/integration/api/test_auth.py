# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Auth, end to end, against the real database.

The properties asserted here are the ones whose absence is a breach, not a bug:
nothing plaintext at rest, no user enumeration, revocation that actually revokes.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import math
import re
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.models.actor import Actor, Scope
from prismoid.db.models.api_key import ApiKey
from prismoid.db.models.session import AuthSession
from prismoid.db.models.user import User
from prismoid.db.models.workspace import WorkspaceMember, WorkspaceRole
from prismoid.db.models.workspace_invite import WorkspaceInvite
from prismoid.db.session import session_scope
from prismoid.errors import Forbidden, NotFound, RateLimited, Unauthorized
from prismoid.operations import auth as ops
from prismoid.schemas.ids import API_KEY_PREFIX, PASSWORD_RESET_PREFIX, SESSION_TOKEN_PREFIX

pytestmark = pytest.mark.integration


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """Just a session. Cleanup is autouse in `tests/conftest.py`, scoped to what this test made -
    a suite that nukes `actors` also nukes the developer's login."""
    async with session_scope() as session:
        yield session


@pytest.fixture(autouse=True)
async def drained_outbox() -> AsyncIterator[None]:
    """Every test ends with a drained outbox: an outbound send still pending when the test's loop
    closes is a destroyed-task warning. (The rate-limit buckets are emptied by conftest.)"""
    yield
    await ops.flush_outbound_email()


def an_email() -> str:
    return f"u{uuid.uuid4().hex[:12]}@example.com"


async def test_signup_logs_you_in_and_hashes_everything(db: AsyncSession) -> None:
    email = an_email()
    result = await ops.signup(db, email=email, name="Ana", password="a-long-enough-password")

    assert result.token.startswith(SESSION_TOKEN_PREFIX)
    assert result.user.email == email

    # Rule 9: the plaintext exists in the response and nowhere else on earth.
    stored = await db.scalar(text("select token_hash from auth_sessions"))
    assert stored != result.token
    assert not str(stored).startswith(SESSION_TOKEN_PREFIX)

    hashed = await db.scalar(text("select password_hash from credentials"))
    assert str(hashed).startswith("$argon2id$")  # not bcrypt, not sha, not plaintext


async def test_a_signup_on_an_empty_users_table_is_not_an_admin(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No web path grants platform admin: the first signup on a fresh box is an ordinary account
    (an attacker watching certificate logs could otherwise sign up first and own the console).
    The first admin comes from `prismoid admin bootstrap` on the box, or `prismoid up`'s first run.

    Tests the rule, not the global state: the empty table is simulated, so the test does not
    depend on nobody having used the product."""

    async def empty(_cls: type[User], _session: AsyncSession) -> int:
        return 0

    monkeypatch.setattr(User, "count", classmethod(empty))
    result = await ops.signup(db, email=an_email(), name="Ana", password="a-long-enough-password")
    actor = await db.get(Actor, result.user.actor_id)
    assert actor is not None and not actor.has_scope(Scope.ADMIN)


async def _actor_for(db: AsyncSession, token: str) -> Actor:
    principal = await AuthSession.resolve(db, token)
    assert principal is not None
    return principal.actor


async def test_signup_is_throttled_per_address(db: AsyncSession) -> None:
    """Each signup mints a workspace and its grant, so one address cannot mint them at
    will: past the window it is refused, while another address - and the same address's
    logins, a different bucket - carry on."""
    limit = math.ceil(settings.auth.login_rate_limit_per_minute / settings.api.replica_count)
    password = "a-long-enough-password"
    for _ in range(limit):
        await ops.signup(db, email=an_email(), name="Burst", password=password, ip="203.0.113.7")
    with pytest.raises(RateLimited):
        await ops.signup(db, email=an_email(), name="Burst", password=password, ip="203.0.113.7")
    elsewhere = an_email()
    await ops.signup(db, email=elsewhere, name="Elsewhere", password=password, ip="203.0.113.8")
    assert (await ops.login(db, email=elsewhere, password=password, ip="203.0.113.7")).user.email == elsewhere


async def test_wrong_address_guesses_at_an_invite_code_spend_the_signup_window(db: AsyncSession) -> None:
    """A leaked invite code is the invitee's alone: each signup that names it with another address
    is refused, and those refusals spend the address's signup window like any signup, so the code
    cannot be tried against address after address at will."""
    owner = await ops.signup(db, email=an_email(), name="Owner", password="a-long-enough-password")
    workspace = await db.scalar(
        select(WorkspaceMember.workspace_id).where(WorkspaceMember.actor_id == owner.user.actor_id)
    )
    assert workspace is not None
    _, code = await WorkspaceInvite.mint(db, workspace, an_email(), WorkspaceRole.MEMBER, owner.user.actor_id)
    await db.commit()
    limit = math.ceil(settings.auth.login_rate_limit_per_minute / settings.api.replica_count)

    async def guess() -> None:
        await ops.signup(
            db,
            email=an_email(),
            name="Guess",
            password="a-long-enough-password",
            ip="203.0.113.9",
            invite_code=code,
        )

    for _ in range(limit):
        with pytest.raises(Forbidden):
            await guess()
    with pytest.raises(RateLimited):
        await guess()


async def test_email_is_normalised_on_the_way_in_and_out(db: AsyncSession) -> None:
    """`Ana@Co.COM` must find the account `ana@co.com` created, or people cannot log in.

    The email is generated, not hard-coded: a fixed address collides with whatever a previous run
    left behind, and a test that only passes on a fresh database is a test that will be deleted.
    """
    raw = an_email()
    await ops.signup(db, email=raw.upper(), name="Ana", password="a-long-enough-password")
    result = await ops.login(db, email=raw.lower(), password="a-long-enough-password")
    assert result.user.email == raw.lower()


async def test_a_wrong_password_and_a_missing_user_are_indistinguishable(db: AsyncSession) -> None:
    """Both raise Unauthorized with the same message. A different error for "no such user" is a
    free account-enumeration oracle."""
    real = an_email()
    await ops.signup(db, email=real, name="Real", password="a-long-enough-password")

    with pytest.raises(Unauthorized) as wrong_password:
        await ops.login(db, email=real, password="not-the-password")
    with pytest.raises(Unauthorized) as no_such_user:
        await ops.login(db, email=an_email(), password="not-the-password")

    assert str(wrong_password.value) == str(no_such_user.value)


async def test_logout_revokes_the_session_immediately(db: AsyncSession) -> None:
    """The kill switch a JWT cannot have."""
    result = await ops.signup(db, email=an_email(), name="Ana", password="a-long-enough-password")
    assert await AuthSession.resolve(db, result.token) is not None

    await ops.logout(db, result.token)
    assert await AuthSession.resolve(db, result.token) is None


async def test_update_me_edits_the_name_in_both_rows(db: AsyncSession) -> None:
    """`users.name` and `actors.display_name` mirror each other at signup; an edit must keep
    them mirrored, or Settings shows one name and event attributions another."""
    result = await ops.signup(db, email=an_email(), name="Ana", password="a-long-enough-password")
    actor = await _actor_for(db, result.token)

    user = await ops.update_me(db, actor, name="Ana Bell", timezone="Europe/Lisbon")
    assert user.name == "Ana Bell"
    assert user.timezone == "Europe/Lisbon"

    refreshed = await db.get(Actor, actor.id)
    assert refreshed is not None and refreshed.display_name == "Ana Bell"

    # A partial PATCH touches only what arrived.
    user = await ops.update_me(db, actor, timezone="UTC")
    assert user.name == "Ana Bell"
    assert user.timezone == "UTC"


async def test_an_api_key_authenticates_until_it_is_revoked(db: AsyncSession) -> None:
    result = await ops.signup(db, email=an_email(), name="Ana", password="a-long-enough-password")
    actor = await _actor_for(db, result.token)

    key, plaintext = await ops.create_api_key(db, actor, "ci")
    assert plaintext.startswith(API_KEY_PREFIX)
    assert await ApiKey.resolve(db, plaintext) is not None

    await ops.revoke_api_key(db, actor, key.id)
    assert await ApiKey.resolve(db, plaintext) is None


async def test_you_cannot_revoke_someone_elses_key_and_asking_tells_you_nothing(
    db: AsyncSession,
) -> None:
    """NotFound, never Forbidden - a 403 would confirm the key exists."""
    mine = await ops.signup(db, email=an_email(), name="Ana", password="a-long-enough-password")
    theirs = await ops.signup(db, email=an_email(), name="Bo", password="a-long-enough-password")

    my_actor = await _actor_for(db, mine.token)
    their_actor = await _actor_for(db, theirs.token)
    their_key, _ = await ops.create_api_key(db, their_actor, "theirs")

    with pytest.raises(NotFound):
        await ops.revoke_api_key(db, my_actor, their_key.id)


async def test_a_garbage_token_is_rejected_without_touching_the_database(db: AsyncSession) -> None:
    """Prefix dispatch: anything that is not `cs_` or `ck_` is 401 on sight."""
    assert await AuthSession.resolve(db, "not-a-token") is None
    assert await ApiKey.resolve(db, "not-a-token") is None
    assert await AuthSession.resolve(db, f"{API_KEY_PREFIX}wrong-kind") is None


# ── forgotten passwords ───────────────────────────────────────────────────────────────────────
# The dev loop the feature exists for: with the console email backend (the default), the code
# is READ OFF THE CONSOLE. These tests exercise exactly that - capsys is the developer's eyes.


async def _code_from_console(capsys: pytest.CaptureFixture[str]) -> str:
    # The send rides a fire-and-forget task off the request path - drain it,
    # or the print this helper reads for may not have happened yet.
    await ops.flush_outbound_email()
    out = capsys.readouterr().out
    match = re.search(rf"{PASSWORD_RESET_PREFIX}[A-Za-z0-9_\-]+", out)
    assert match, f"no reset code printed on the console:\n{out}"
    return match.group(0)


async def test_forgot_prints_the_code_and_reset_rotates_the_password_and_kills_sessions(
    db: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    email = an_email()
    first = await ops.signup(db, email=email, name="Ana", password="the-original-password")

    await ops.request_password_reset(db, email=email)
    code = await _code_from_console(capsys)

    # Rule 9: the plaintext exists in the email and nowhere else - the row stores a hash.
    stored = await db.scalar(text("select code_hash from password_resets order by created_at desc limit 1"))
    assert stored != code
    assert not str(stored).startswith(PASSWORD_RESET_PREFIX)

    await ops.reset_password(db, code=code, new_password="the-brand-new-password")

    # The old session died with the old password - whoever held it is out.
    assert await AuthSession.resolve(db, first.token) is None

    # The old password is refused; the new one works.
    with pytest.raises(Unauthorized):
        await ops.login(db, email=email, password="the-original-password")
    result = await ops.login(db, email=email, password="the-brand-new-password")
    assert result.user.email == email


async def test_a_reset_code_is_single_use(db: AsyncSession, capsys: pytest.CaptureFixture[str]) -> None:
    email = an_email()
    await ops.signup(db, email=email, name="Ana", password="the-original-password")
    await ops.request_password_reset(db, email=email)
    code = await _code_from_console(capsys)

    await ops.reset_password(db, code=code, new_password="the-brand-new-password")
    with pytest.raises(Unauthorized):
        await ops.reset_password(db, code=code, new_password="a-second-try-password")


async def test_a_new_request_supersedes_the_old_code(
    db: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    email = an_email()
    await ops.signup(db, email=email, name="Ana", password="the-original-password")

    await ops.request_password_reset(db, email=email)
    old_code = await _code_from_console(capsys)
    await ops.request_password_reset(db, email=email)
    new_code = await _code_from_console(capsys)

    # At most one key to the front door exists at a time.
    with pytest.raises(Unauthorized):
        await ops.reset_password(db, code=old_code, new_password="the-brand-new-password")
    await ops.reset_password(db, code=new_code, new_password="the-brand-new-password")


async def test_an_unknown_email_is_the_same_quiet_answer_and_mints_nothing(
    db: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operation returns None either way: no exception, no email, no row - no oracle."""
    await ops.request_password_reset(db, email=an_email())
    await ops.flush_outbound_email()
    assert PASSWORD_RESET_PREFIX not in capsys.readouterr().out


async def test_an_expired_code_is_dead_and_eventually_swept(
    db: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    email = an_email()
    result = await ops.signup(db, email=email, name="Ana", password="the-original-password")
    await ops.request_password_reset(db, email=email)
    code = await _code_from_console(capsys)

    await db.execute(
        text("update password_resets set expires_at = now() - interval '1 hour' where user_id = :uid"),
        {"uid": result.user.actor_id},
    )
    await db.commit()

    with pytest.raises(Unauthorized):
        await ops.reset_password(db, code=code, new_password="the-brand-new-password")

    # And the sweeper's operation reaps it once the grace has passed (here: no grace).
    assert await ops.purge_dead_password_resets(db, grace=dt.timedelta(0)) >= 1


async def test_reset_requests_throttle_per_user_but_never_error(
    db: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    """Request past the hourly cap: still silent 202s, but nothing new is minted or mailed."""
    email = an_email()
    result = await ops.signup(db, email=email, name="Ana", password="the-original-password")

    cap = settings.auth.password_reset_max_per_hour
    for _ in range(cap + 2):
        await ops.request_password_reset(db, email=email)
    await ops.flush_outbound_email()

    minted = await db.scalar(
        text("select count(*) from password_resets where user_id = :uid"),
        {"uid": result.user.actor_id},
    )
    assert minted == cap
    assert capsys.readouterr().out.count("Your Prismoid password reset code") == cap


async def test_reset_revokes_api_keys_too(db: AsyncSession, capsys: pytest.CaptureFixture[str]) -> None:
    """A `ck_` key minted by whoever knew the old password must not survive the recovery
    : sessions-only revocation leaves the takeover standing."""
    email = an_email()
    result = await ops.signup(db, email=email, name="Ana", password="the-original-password")
    actor = await _actor_for(db, result.token)
    _, plaintext = await ops.create_api_key(db, actor, "minted-while-in")
    assert await ApiKey.resolve(db, plaintext) is not None

    await ops.request_password_reset(db, email=email)
    code = await _code_from_console(capsys)
    await ops.reset_password(db, code=code, new_password="the-brand-new-password")

    assert await ApiKey.resolve(db, plaintext) is None


async def test_concurrent_requests_leave_exactly_one_live_code(
    db: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    """'At most one key to the front door' must hold under a double-click, not just in sequence:
    without the user-row lock, two interleaved transactions each see 'nothing pending' and both
    mint - two simultaneously-live codes."""
    email = an_email()
    result = await ops.signup(db, email=email, name="Ana", password="the-original-password")

    async def one_request() -> None:
        async with session_scope() as own:
            await ops.request_password_reset(own, email=email)

    await asyncio.gather(one_request(), one_request())
    await ops.flush_outbound_email()

    live = await db.scalar(
        text("select count(*) from password_resets where user_id = :uid and code_hash is not null"),
        {"uid": result.user.actor_id},
    )
    assert live == 1


async def test_a_failed_send_never_consumes_the_reset_cap(
    db: AsyncSession, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SMTP outage: still quiet 202s, but a row whose email never left the building is retired -
    a dead relay must not spend the 5/hour allowance and lock the user out AFTER mail recovers."""
    email = an_email()
    result = await ops.signup(db, email=email, name="Ana", password="the-original-password")

    async def dead_relay(*, to: str, subject: str, text: str) -> bool:
        return False

    monkeypatch.setattr(ops.mailer, "send", dead_relay)
    for _ in range(settings.auth.password_reset_max_per_hour):
        await ops.request_password_reset(db, email=email)
    await ops.flush_outbound_email()

    remaining = await db.scalar(
        text("select count(*) from password_resets where user_id = :uid"),
        {"uid": result.user.actor_id},
    )
    assert remaining == 0

    monkeypatch.undo()  # the relay recovers - and the user is not locked out
    await ops.request_password_reset(db, email=email)
    code = await _code_from_console(capsys)
    assert code.startswith(PASSWORD_RESET_PREFIX)


async def test_forgot_password_goes_quiet_when_hammered_and_never_locks_login(
    db: AsyncSession,
) -> None:
    """The contract is an unconditional 202: past the in-process window the operation goes
    quiet - it never raises RateLimited. And its bucket is namespaced, so the burst must not
    throttle the same person's login (the user recovering IS the user who just failed logins)."""
    email = an_email()
    limit = math.ceil(settings.auth.login_rate_limit_per_minute / settings.api.replica_count)
    for _ in range(limit + 3):
        await ops.request_password_reset(db, email=email)  # unknown email: quiet and cheap

    # Login's own bucket is untouched - Unauthorized (wrong password), never RateLimited.
    with pytest.raises(Unauthorized):
        await ops.login(db, email=email, password="not-a-real-password")
