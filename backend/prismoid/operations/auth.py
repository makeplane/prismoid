# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The auth verbs. Operations own transactions; views never start one.

Two hashes, two reasons, and they are not interchangeable:

* **passwords → argon2id.** Human-chosen, low-entropy, attackable with a dictionary. The whole
  point is to be slow. OWASP's first choice; bcrypt's 72-byte truncation avoided; passlib
  rejected on maintenance risk.
* **tokens → SHA-256.** 32 bytes of `secrets`-grade randomness. There is no dictionary. A slow
  hash here would buy nothing and would cost argon2 on *every authenticated request*.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import secrets
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

import logfire
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import mailer
from prismoid.conf import settings
from prismoid.db.base import rows_affected, utcnow
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.api_key import ApiKey
from prismoid.db.models.credential import Credential
from prismoid.db.models.email_verification import EmailVerification
from prismoid.db.models.password_reset import PasswordReset
from prismoid.db.models.session import AuthSession
from prismoid.db.models.user import User
from prismoid.db.models.workspace import Workspace, WorkspaceKind, WorkspaceMember, WorkspaceRole
from prismoid.db.session import session_scope
from prismoid.errors import (
    Forbidden,
    NotFound,
    PrismoidError,
    RateLimited,
    SignupDisabled,
    Unauthorized,
    ValidationFailed,
)
from prismoid.operations import allowance, throttle, workspaces
from prismoid.operations.throttle import Bucket
from prismoid.schemas.auth import AuthConfig, SignupMode
from prismoid.schemas.ids import PASSWORD_RESET_PREFIX, hash_token

_hasher = PasswordHasher(
    time_cost=settings.auth.argon2_time_cost,
    memory_cost=settings.auth.argon2_memory_cost_kib,
    parallelism=settings.auth.argon2_parallelism,
)

# A hash of nothing, verified against when the email is unknown. Without it, "no such user"
# returns in microseconds and "wrong password" takes 50 ms - and the difference is a free user
# enumeration oracle for anyone with a stopwatch.
_DUMMY_HASH = _hasher.hash("a password that is not anyone's")


@dataclass(frozen=True)
class LoginResult:
    token: str  # plaintext, exists only here and in the response
    expires_at: dt.datetime
    user: User


# ── rate limiting (operations/throttle's durable buckets) ─────────────────────────────────────
# Each door counts per account (the normalized address, hashed: the table holds no one's email)
# and per client address, under its own prefix, so one door's burst never locks a different door:
# the person who just burned their login window can still ask for a reset code.
#
# Login's account bucket is the account AS SEEN FROM one client address. Keyed on the account
# alone, its line and its backoff refuse before the password is checked, so anyone who knows an
# email could keep its owner locked out for good with one wrong guess each time the block lapsed.
# Checking the password first instead would let a right guess in during the backoff, which leaves
# the backoff guarding nothing. Per (account, address), a guesser still meets the backoff before
# the hash is checked, and can lock out only their own address; the address bucket still caps one
# address across every account (credential stuffing), with its own, higher backoff.


def _account(door: str, email: str) -> str:
    return f"{door}:acct:{hash_token(User.normalize_email(email))}"


def _address(door: str, ip: str) -> str:
    return f"{door}:ip:{ip}"


def _login_buckets(email: str, ip: str) -> tuple[Bucket, Bucket]:
    auth = settings.auth
    return (
        Bucket(
            f"{_account('login', email)}:ip:{hash_token(ip)}",  # hashed: a fixed length
            auth.login_rate_limit_per_minute,
            auth.login_backoff_after_failures,
        ),
        Bucket(
            _address("login", ip), auth.login_ip_rate_limit_per_minute, auth.login_ip_backoff_after_failures
        ),
    )


# ── the verbs ─────────────────────────────────────────────────────────────────────────────────


def public_config() -> AuthConfig:
    """What a signed-out page may know about this deployment (ADR-086): the signup mode and the
    legal and support links. Settings only; nothing about any account."""
    api = settings.api
    return AuthConfig(
        signup=SignupMode.OPEN if settings.auth.signup_enabled else SignupMode.INVITE,
        terms_url=api.terms_url or None,
        privacy_url=api.privacy_url or None,
        support_email=api.support_email or None,
    )


async def signup(
    session: AsyncSession,
    *,
    email: str,
    name: str,
    password: str,
    ip: str = "",
    user_agent: str = "",
    invite_code: str | None = None,
    local_first_run: bool = False,
) -> LoginResult:
    """Create the human and their personal workspace (owner membership), and log them in, active in
    that workspace. One transaction. The address is proved by an emailed `ev_` code (`verify_email`);
    an invite to this address, or `prismoid up`'s first run, proves it here at once. A proved address
    is what `allowance.address_proved` hears. With an `invite_code`
    the invite is accepted in the same transaction and the INVITING workspace is the active one; a bad
    code or another address refuses the whole signup (nothing is created).

    With `signup_enabled` off (hosted, invite-only) a live invite for THIS address is the way in; it
    is checked before any row is made. `local_first_run` is the other bypass, for `prismoid up`'s first
    run: an argument of THIS call, never a setting, so no concurrent request can sign up
    through it. Only local mode honours it."""
    local_first = local_first_run and settings.environment == "local"
    if not settings.auth.signup_enabled and not invite_code and not local_first:
        raise SignupDisabled("Signups are paused.")
    # Every signup mints an account and a workspace: one client address, and one address
    # signing up, get a few a minute. Ahead of the invite check, so a leaked code cannot be tried
    # against address after address unthrottled.
    per_minute = settings.auth.signup_rate_limit_per_minute
    await throttle.take(
        Bucket(_address("signup", ip), per_minute),
        Bucket(_account("signup", email), per_minute),
        message="too many signups",
    )
    invite = await workspaces.check_invite(session, email, invite_code) if invite_code else None

    if await User.by_email(session, email) is not None:
        # Deliberately the same shape as a successful signup's failure mode elsewhere: we do not
        # confirm which emails exist to an unauthenticated caller.
        raise Forbidden("that email cannot be registered")

    # The platform admin scope comes only with `prismoid up`'s first run here, never with an empty
    # users table: the first admin of a hosted box is made on the box (`prismoid admin bootstrap`).
    user, workspace = await create_account(
        session, email=email, name=name, password_hash=_hasher.hash(password), admin=local_first
    )
    active, role = workspace, WorkspaceRole.OWNER.value
    if invite is not None:
        active = await workspaces.join_by_invite(session, user, invite)
        role = invite.role
        tenant.bind(session, active.id, role)
    verification: tuple[EmailVerification, str] | None = None
    if invite is not None or local_first:
        user.email_verified_at = utcnow()
        await allowance.address_proved(session, user)
    else:
        verification = EmailVerification.issue(user.actor_id)
        session.add(verification[0])
    auth_session, raw = AuthSession.issue(user.actor_id, user_agent)
    auth_session.active_workspace_id = active.id
    session.add(auth_session)
    await session.commit()
    if verification is not None:  # after commit: the mail leaves only once its row is durable
        row, code = verification
        send_in_background(_deliver_verification_code(row.id, to=user.email, name=user.name, code=code))
    return LoginResult(token=raw, expires_at=auth_session.expires_at, user=user)


async def create_account(
    session: AsyncSession, *, email: str, name: str, password_hash: str, admin: bool = False
) -> tuple[User, Workspace]:
    """The human (Actor + User + Credential) and their personal workspace, bound as its owner.
    Flushed, NOT committed: the caller owns the transaction."""
    user = await User.signup(session, email=email, name=name, password_hash=password_hash, admin=admin)
    actor = await session.get(Actor, user.actor_id)
    if actor is None:  # User.signup flushed the actor it created; an assert vanishes under -O
        raise PrismoidError("signup created no actor - refusing to make an ownerless workspace")
    workspace = await workspaces.create_workspace(
        session, actor, name=f"{name}'s workspace", kind=WorkspaceKind.PERSONAL
    )
    tenant.bind(session, workspace.id, WorkspaceRole.OWNER.value)
    return user, workspace


def unusable_password_hash() -> str:
    """An argon2id hash of 32 random bytes nobody ever saw: an account that exists but cannot be
    logged into until its owner sets a password through a reset code."""
    return _hasher.hash(secrets.token_urlsafe(32))


async def login(
    session: AsyncSession, *, email: str, password: str, ip: str = "", user_agent: str = ""
) -> LoginResult:
    buckets = _login_buckets(email, ip)
    await throttle.take(*buckets, message="too many login attempts")

    user = await User.by_email(session, email)
    credential = await session.get(Credential, user.actor_id) if user is not None else None

    # Verify *something*, always - see _DUMMY_HASH. The branch below must take the same time
    # whether or not the account exists, and both failures count toward the backoff alike.
    stored = credential.password_hash if credential is not None else _DUMMY_HASH
    try:
        _hasher.verify(stored, password)
        verified = user is not None and credential is not None
    except (VerifyMismatchError, VerificationError):
        verified = False
    if not verified or user is None or credential is None:
        await throttle.failed(*buckets)
        raise Unauthorized("invalid email or password")
    await throttle.succeeded(buckets[0])  # the account's run ends; the address keeps its count

    # The kill switch holds at the front door too (admin surface, D4): suspension revokes every
    # live session and key, and `require_actor` refuses what remains - but without this check a
    # suspended person could mint a FRESH session that 403s on every use. Refusing after the
    # password verified leaks nothing: they proved the identity; the account is simply off.
    actor = await session.get(Actor, user.actor_id)
    if actor is not None and not actor.is_active():
        raise Forbidden("account suspended")

    # argon2 parameters change (tuning them is routine). A login is the one moment we hold the plaintext,
    # so it is the only moment we can re-hash - otherwise a parameter bump locks everyone out.
    if _hasher.check_needs_rehash(credential.password_hash):
        credential.password_hash = _hasher.hash(password)

    auth_session, raw = AuthSession.issue(user.actor_id, user_agent)
    # The session opens in the person's earliest membership - normally their personal workspace. None
    # (no membership left) mints a session that only the way back in admits: it can read `me`,
    # create or join a workspace, and log out; every other route answers 403, the truthful sentence.
    earliest = await WorkspaceMember.earliest_for(session, user.actor_id)
    auth_session.active_workspace_id = earliest.workspace_id if earliest is not None else None
    session.add(auth_session)
    await session.commit()
    return LoginResult(token=raw, expires_at=auth_session.expires_at, user=user)


async def logout(session: AsyncSession, raw_token: str) -> None:
    await AuthSession.revoke(session, raw_token)
    await session.commit()


# How long a dead session (expired OR revoked) lingers before the sweeper hard-deletes it. A
# grace window, not zero: a just-dead row is occasionally useful for support/audit, and a week
# keeps that without letting the table grow unbounded.
SESSION_GC_GRACE = dt.timedelta(days=7)


async def purge_expired_sessions(session: AsyncSession, *, grace: dt.timedelta = SESSION_GC_GRACE) -> int:
    """Hard-delete auth sessions dead (expired OR revoked) longer than `grace`. Returns the
    count removed.

    Opaque, revocable Postgres sessions are the whole reason we rejected JWT - but a revocable row
    is a row that accumulates, and nothing was ever reaping the dead ones. Idempotent by
    construction: the set to delete is defined by the clock, so a second sweep finds only what has
    newly aged out. `revoked_at` is nullable, so `NULL < cutoff` is false and a live session is
    never touched. The FK is ON DELETE CASCADE from users; this only ever removes session rows.
    """
    cutoff = utcnow() - grace
    result = await session.execute(
        delete(AuthSession).where((AuthSession.expires_at < cutoff) | (AuthSession.revoked_at < cutoff))
    )
    await session.commit()
    return rows_affected(result)


# ── forgotten passwords ───────────────────────────────────────────────────────────────────────

# How long a dead reset row (consumed, superseded, or simply never used) lingers as audit trail
# before the sweeper hard-deletes it. Long enough for "someone reset my password last week" to
# be answerable from the rows; short enough that the table never becomes a biography.
RESET_GC_GRACE = dt.timedelta(days=30)

# Outbound mail runs OFF the request path. Awaited inline, the SMTP round
# trip made a known email's 202 wait on the relay (up to smtp_timeout_seconds) while an unknown
# email returned from one SELECT - a user-enumeration stopwatch, the exact oracle _DUMMY_HASH
# exists to close - and a relay hiccup could stall or (via a raise) 500 work that had already
# committed. Sends are fire-and-forget tasks on the serving loop, held strongly here (a bare
# create_task result can be garbage-collected mid-flight), drained at API shutdown and by the
# tests. Deliberately NOT a DBOS job: the plaintext code must never ride job arguments,
# so the send stays in the process holding the only copy.
_outbound: set[asyncio.Task[None]] = set()


async def _guarded(coro: Coroutine[Any, Any, object]) -> None:
    """An exception in a fire-and-forget task otherwise surfaces only at garbage collection."""
    try:
        await coro
    except Exception:
        logfire.exception("outbound email task failed")


def send_in_background(coro: Coroutine[Any, Any, object]) -> None:
    task = asyncio.create_task(_guarded(coro))
    _outbound.add(task)
    task.add_done_callback(_outbound.discard)


async def flush_outbound_email() -> None:
    """Await every in-flight send - the API's shutdown drain and the tests' determinism seam."""
    while _outbound:
        await asyncio.gather(*tuple(_outbound), return_exceptions=True)


async def request_password_reset(session: AsyncSession, *, email: str, ip: str = "") -> None:
    """Mint and email a single-use reset code - and say nothing about whether the email exists.

    Returns None on every path (acknowledgement, never an oracle - the anti-enumeration
    doctrine that `signup` and `login` already follow). Four quiet exits: unknown email, either
    throttle, a mailer failure - each logged, none distinguishable from the outside, and none an
    exception (the endpoint's contract is an unconditional 202). The plaintext code exists in
    the email body and nowhere else; the row stores its SHA-256. The send itself runs after the
    response, off the request path (see `send_in_background` above).

    A person-level act, in no one workspace: the unit of work is PLATFORM.
    """
    tenant.bind_platform(session)
    auth = settings.auth
    try:
        # Its own buckets: sharing login's would lock the user who just burned ten failed logins
        # out of the recovery flow that exists for exactly that moment.
        await throttle.take(
            Bucket(_account("reset", email), auth.reset_rate_limit_per_minute),
            Bucket(_address("reset", ip), auth.login_ip_rate_limit_per_minute),
            message="too many reset requests",
        )
    except RateLimited:
        # A 429 would break the always-202 contract AND narrate that this address is being
        # hammered. Quiet, logged, done - the same shape as the durable throttle below.
        logfire.warning("password reset request rate-limited")
        return

    user = await User.by_email(session, email)
    if user is None:
        logfire.info("password reset requested for an unknown email")
        return

    # Serialize concurrent minters on the user row: under READ COMMITTED two interleaved
    # requests each see "nothing pending", each supersede nothing, and each mint - two
    # simultaneously-live keys to one front door, and a parallel burst overshoots the mail
    # cap. The lock rides the existing transaction; three short statements later it commits.
    await User.lock_row(session, user.actor_id)

    window = dt.timedelta(hours=1)
    if await PasswordReset.recent_count(session, user.actor_id, window) >= (
        settings.auth.password_reset_max_per_hour
    ):
        logfire.warning("password reset throttled for user {user_id}", user_id=str(user.actor_id))
        return

    superseded = await PasswordReset.supersede_pending(session, user.actor_id)
    row, code = PasswordReset.issue(user.actor_id)
    session.add(row)
    await session.commit()

    logfire.info(
        "password reset {reset_id} issued for user {user_id} "
        "(expires {expires_at}, {superseded} superseded, email queued)",
        reset_id=str(row.id),
        user_id=str(user.actor_id),
        expires_at=row.expires_at.isoformat(),
        superseded=superseded,
    )
    # After commit, deliberately (Rule 4's enqueue-after-commit shape): the email leaves the
    # building only once the row it references is durable.
    send_in_background(_deliver_reset_code(row.id, to=user.email, name=user.name, code=code))


async def _deliver_reset_code(reset_id: uuid.UUID, *, to: str, name: str, code: str) -> None:
    """The background half of `request_password_reset`: send, and if the send failed, un-mint.

    A row whose email never left the building must not count against the 5/hour cap - an SMTP
    outage would otherwise spend the whole allowance on quiet 202s and keep the user locked out
    for up to an hour AFTER mail recovers. The failed row is deleted (its code is undeliverable,
    so nothing usable is destroyed); the warning line keeps the trail.
    """
    ttl = settings.auth.password_reset_ttl_minutes
    sent = await mailer.send(
        to=to,
        subject="Your Prismoid password reset code",
        text=(
            f"Hi {name},\n"
            f"\n"
            f"Someone asked to reset the password for {to} - hopefully you.\n"
            f"Paste this one-time code into the reset form:\n"
            f"\n"
            f"    {code}\n"
            f"\n"
            f"It expires in {ttl} minutes and works exactly once. If you didn't ask,\n"
            f"ignore this email - your password is unchanged."
        ),
    )
    if sent:
        logfire.info("password reset {reset_id} email sent", reset_id=str(reset_id))
        return
    async with session_scope(platform=True) as session:  # a person-level row, no workspace's
        await session.execute(delete(PasswordReset).where(PasswordReset.id == reset_id))
        await session.commit()
    logfire.warning(
        "password reset {reset_id} email delivery failed - row retired so a dead relay "
        "never consumes the per-user cap",
        reset_id=str(reset_id),
    )


async def _deliver_verification_code(verification_id: uuid.UUID, *, to: str, name: str, code: str) -> None:
    """Send the verification mail; if the send failed, un-mint the row so a dead relay never spends
    the hourly cap. The link carries the code in the URL fragment, which never reaches a server log;
    with no web address configured the mail shows the code to paste."""
    base = settings.api.web_base_url.rstrip("/")
    how = (
        f"Open this link to verify:\n\n    {base}/verify-email#{code}"
        if base
        else f"Your code:\n\n    {code}"
    )
    ttl_hours = settings.auth.email_verification_ttl_minutes // 60
    sent = await mailer.send(
        to=to,
        subject="Verify your email for Prismoid",
        text=(
            f"Hi {name},\n\n"
            f"{how}\n\n"
            f"It works once and expires in {ttl_hours} hours. If you didn't sign up, ignore this email."
        ),
    )
    if sent:
        logfire.info("email verification {verification_id} sent", verification_id=str(verification_id))
        return
    async with session_scope(platform=True) as session:
        await session.execute(delete(EmailVerification).where(EmailVerification.id == verification_id))
        await session.commit()
    logfire.warning(
        "email verification {verification_id} delivery failed - row retired",
        verification_id=str(verification_id),
    )


async def verify_email(session: AsyncSession, actor: Actor, code: str) -> User:
    """Prove the caller's address with the emailed code (`allowance.address_proved` hears it). Already
    verified is a quiet success: no code touched, nothing heard twice, so a replayed link or a double
    click is harmless. The user row lock makes two racing claims one: the second sees it verified."""
    user = await me(session, actor)
    await session.refresh(user, with_for_update=True)
    if user.email_verified_at is not None:
        return user
    if not await EmailVerification.consume(session, hash_token(code), user.actor_id):
        # 422, never 401: a 401 means a dead session to the web client, which signs out
        raise ValidationFailed(
            "that link has expired or was already used",
            {"code": "That link has expired or was already used."},
        )
    user.email_verified_at = utcnow()
    await allowance.address_proved(session, user)
    await session.commit()
    return user


async def resend_verification(session: AsyncSession, actor: Actor, *, ip: str = "") -> None:
    """Mail a fresh code, retiring the pending one. A verified person has nothing to prove, so
    nothing is sent. Capped per user per hour (durable) and per minute (the door's buckets)."""
    user = await me(session, actor)
    auth = settings.auth
    await throttle.take(
        Bucket(_account("verify", user.email), auth.reset_rate_limit_per_minute),
        Bucket(_address("verify", ip), auth.login_ip_rate_limit_per_minute),
        message="too many verification requests",
    )
    await session.refresh(user, with_for_update=True)
    if user.email_verified_at is not None:
        return
    if await EmailVerification.recent_count(session, user.actor_id, dt.timedelta(hours=1)) >= (
        auth.email_verification_max_per_hour
    ):
        raise RateLimited("too many verification emails; try again later", retry_after=3600)
    await EmailVerification.supersede_pending(session, user.actor_id)
    row, code = EmailVerification.issue(user.actor_id)
    session.add(row)
    await session.commit()
    send_in_background(_deliver_verification_code(row.id, to=user.email, name=user.name, code=code))


async def reset_password(session: AsyncSession, *, code: str, new_password: str, ip: str = "") -> None:
    """Trade a live reset code for a new password. The code is the whole authentication.

    One atomic claim (match + TTL + clear, in the model), then the writes that must land
    together or not at all: the new argon2id hash, the code consumed, **every live session
    revoked**, and **every live API key revoked** - whoever knew the old password may be holding
    either open right now (any session can mint a `ck_` key, and a key that survives the reset
    is a takeover that survives the recovery). One error message for every failure mode:
    invalid, expired, replayed, and superseded must be indistinguishable to the caller.

    PLATFORM: the person's sessions and API keys are revoked in EVERY workspace,
    by actor id - a reset that spared one workspace's keys would not have taken the account back.
    """
    tenant.bind_platform(session)
    # A per-address speed bump against online code-guessing; the code's 256 bits are the real
    # wall. Not the login bucket, and not a global constant - a constant key collapses to ONE
    # fleet-wide bucket wherever the proxy hides client addresses (see
    # `ApiSettings.forwarded_allow_ips`), handing an unauthenticated attacker a standing DoS on the
    # recovery door.
    await throttle.take(
        Bucket(_address("reset-complete", ip), settings.auth.reset_rate_limit_per_minute),
        message="too many reset attempts",
    )

    claimed = None
    if code.startswith(PASSWORD_RESET_PREFIX):
        claimed = await PasswordReset.consume(session, hash_token(code))
    if claimed is None:
        raise Unauthorized("invalid or expired reset code")

    user = await session.get(User, claimed.user_id)
    if user is None or user.deleted_at is not None:
        # A code minted before a GDPR erasure must not reanimate the account.
        raise Unauthorized("invalid or expired reset code")

    credential = await session.get(Credential, claimed.user_id)
    if credential is None:
        # Signup always creates one; a reset that finds none still must not 500 a locked-out user.
        session.add(Credential(user_id=claimed.user_id, password_hash=_hasher.hash(new_password)))
    else:
        credential.password_hash = _hasher.hash(new_password)

    revoked = await AuthSession.revoke_all_for(session, claimed.user_id)
    # users.actor_id IS actors.id (one spine), so the user's keys hang off the same id.
    keys_revoked = await ApiKey.revoke_all_for(session, claimed.user_id)
    await session.commit()
    # The person who proved the account by its emailed code logs straight in with the new
    # password: whatever backoff a guesser earned against it, from any address, ends here.
    await throttle.forget(f"{_account('login', user.email)}:")
    logfire.info(
        "password reset {reset_id} completed for user {user_id} "
        "({revoked} session(s), {keys} API key(s) revoked)",
        reset_id=str(claimed.id),
        user_id=str(claimed.user_id),
        revoked=revoked,
        keys=keys_revoked,
    )

    # The courtesy notice every provider sends, because the person who should read it is the one
    # who did NOT do this. Best-effort by design, and off the request path - a slow relay must
    # not stall (or, via a raise, 500) a reset that has already committed.
    send_in_background(
        mailer.send(
            to=user.email,
            subject="Your Prismoid password was changed",
            text=(
                f"Hi {user.name},\n"
                f"\n"
                f"Your password was just changed. Every signed-in session was signed out and\n"
                f"every API key was revoked.\n"
                f"If this was you, you're all set - sign in with your new password.\n"
                f"If it wasn't, run the forgot-password flow now to take the account back."
            ),
        )
    )


async def purge_dead_email_verifications(
    session: AsyncSession, *, grace: dt.timedelta = RESET_GC_GRACE
) -> int:
    """Hard-delete verification rows long past expiry; `expires_at` is when each stopped mattering."""
    result = await session.execute(
        delete(EmailVerification).where(EmailVerification.expires_at < utcnow() - grace)
    )
    await session.commit()
    return rows_affected(result)


async def purge_dead_password_resets(session: AsyncSession, *, grace: dt.timedelta = RESET_GC_GRACE) -> int:
    """Hard-delete reset rows long past expiry. Returns the count removed.

    One clock for every state: consumed, superseded, or never used, `expires_at` is when the row
    stopped mattering, and `grace` past it the trail has served its purpose. Idempotent by
    construction, same as `purge_expired_sessions` above.
    """
    cutoff = utcnow() - grace
    result = await session.execute(delete(PasswordReset).where(PasswordReset.expires_at < cutoff))
    await session.commit()
    return rows_affected(result)


async def me(session: AsyncSession, actor: Actor) -> User:
    """The API says *user*, never *actor*. This is where the vocabulary is enforced."""
    user = await session.get(User, actor.id)
    if user is None or user.deleted_at is not None:
        raise NotFound("no user for this actor")
    return user


async def whoami(session: AsyncSession, actor: Actor) -> tuple[User, Workspace | None, str | None, bool]:
    """The profile, and the workspace the door bound for this request with the caller's role there
    - `/v1/auth/me` and the workspace switch both answer with it. No workspace at all for a person
    whose every membership is gone: `me` is how the web learns to offer them a way back in. The last
    element is whether they may found another team workspace (the cap, ADR-086 section 2)."""
    user = await me(session, actor)
    can_create = await workspaces.can_found(session, actor)
    workspace_id = tenant.bound_workspace(session)
    if workspace_id is None:
        return user, None, None, can_create
    workspace = await Workspace.get(session, workspace_id)
    role = tenant.role_of(session)
    if workspace is None or role is None:  # the door bound a live membership; a race is a 404, not a 500
        raise NotFound("no active workspace")
    return user, workspace, role, can_create


async def update_me(
    session: AsyncSession, actor: Actor, *, name: str | None = None, timezone: str | None = None
) -> User:
    """Edit the profile - only the fields that arrived. The name lives in two rows -
    `users.name` and `actors.display_name` mirror each other at signup - so a name edit
    writes both, or Settings and the event attributions drift apart."""
    user = await me(session, actor)
    if name is not None:
        user.name = name
        actor.display_name = name  # same session as the view's (deps are request-cached)
    if timezone is not None:
        user.timezone = timezone
    await session.commit()
    return user


async def create_api_key(session: AsyncSession, actor: Actor, name: str) -> tuple[ApiKey, str]:
    """Returns the row and the plaintext. The plaintext is shown exactly once, ever. The key
    belongs to the workspace the caller is working in - it never follows a later switch."""
    key, raw = ApiKey.issue(actor.id, name, workspace_id=tenant.workspace_of(session))
    session.add(key)
    await session.commit()
    return key, raw


async def list_api_keys(session: AsyncSession, actor: Actor) -> list[ApiKey]:
    return await ApiKey.list_for(session, actor.id)


async def revoke_api_key(session: AsyncSession, actor: Actor, key_id: uuid.UUID) -> None:
    """NotFound, not Forbidden, for someone else's key: existence is never leaked."""
    if not await ApiKey.revoke(session, actor.id, key_id):
        raise NotFound("no such key")
    await session.commit()
