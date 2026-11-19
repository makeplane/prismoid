# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""AuthSession - a User's revocable login token, stored as a hash, with an expiry.

Opaque and revocable, never a JWT: statelessness is exactly the property you do not want
in a product with a logout button and a kill switch. The plaintext `cs_…` is shown to the client
once and never stored - what is stored is a SHA-256 of it, which is what `resolve` looks up.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import replace

from sqlalchemy import ColumnElement, ForeignKey, String, and_, column, delete, select, table, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.conf import settings
from prismoid.db.base import Base, created_at, utcnow, uuid_pk
from prismoid.db.models.actor import Actor
from prismoid.db.models.workspace import Workspace, WorkspaceMember, WorkspaceStatus
from prismoid.schemas.ids import SESSION_TOKEN_PREFIX, hash_token, new_session_token
from prismoid.schemas.principal import Principal

# The phones a session registered for push (`models/push_device.py`, ADR-100) end with it. Named by
# table, not by model: the push device model reads this one, and two models must not import each other.
_PUSH_DEVICES = table("push_devices", column("session_id"))


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    # Indexed and unique: this is the hot path of every authenticated request.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.actor_id", ondelete="CASCADE"), nullable=False, index=True
    )
    expires_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    user_agent: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    # The workspace this browser session acts in; SET NULL so a deleted workspace never strands it.
    active_workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[dt.datetime] = created_at()

    def is_live(self) -> bool:
        """Live = not revoked and not expired. Both are checked on every request, in Postgres -
        which is the whole argument against JWT, in one method."""
        return self.revoked_at is None and self.expires_at > utcnow()

    @classmethod
    def live_clause(cls) -> ColumnElement[bool]:
        """`is_live` as a WHERE clause, on the same clock: the one definition every query that
        counts, revokes or pushes to live sessions uses, so none of them drifts from the door."""
        return and_(cls.revoked_at.is_(None), cls.expires_at > utcnow())

    @classmethod
    async def resolve(cls, session: AsyncSession, raw: str) -> Principal[Actor] | None:
        """Raw `cs_…` → the underlying **Actor** and the workspace it acts in, or None. The ONLY
        session fetch path by token (`live_principal` is its twin by id).

        Resolves to the Actor, not the User: auth resolves to the same identity agents and
        services use, so attribution never forks. One round-trip (`_principal_for`).

        A session whose active workspace is no longer valid (the membership was removed, the workspace
        suspended) is REBOUND to the person's earliest remaining membership and the rebind is
        written to the row - the failure path only. With no membership left the
        principal carries no workspace, and only the way back in (`deps.OrglessActorDep`, `OrglessSessionActorDep`) admits it.
        """
        if not raw.startswith(SESSION_TOKEN_PREFIX):
            return None
        found = await cls._principal_for(session, cls.token_hash == hash_token(raw))
        if found is None:
            return None
        auth_session, principal = found
        if principal.workspace_id is not None:
            return principal
        member = await WorkspaceMember.earliest_for(
            session, principal.actor.id
        )  # the rebind - failure path only
        if member is None:
            return principal
        await _persist_rebind(auth_session.id, member.workspace_id)
        return replace(principal, workspace_id=member.workspace_id, role=member.role)

    @classmethod
    async def live_principal(cls, session: AsyncSession, session_id: uuid.UUID) -> Principal[Actor] | None:
        """`resolve`'s twin by row id: the screen socket's proof holds the id its ticket
        named, never the token. The same query, checks and principal as `resolve`
        (`_principal_for`), as a proof - so a suspended actor is a None too.

        It never rebinds. A proof is a read, and a session whose active workspace's membership is gone
        acts nowhere the ticket was minted for: `resolve` would rebind it to another workspace, which
        the proof's workspace fence refuses anyway. So that is a None too, and no write."""
        found = await cls._principal_for(session, cls.id == session_id, proof=True)
        return None if found is None else found[1]

    @classmethod
    async def _principal_for(
        cls, session: AsyncSession, where: ColumnElement[bool], *, proof: bool = False
    ) -> tuple[AuthSession, Principal[Actor]] | None:
        """The live session and the principal it proves - in the session's active workspace, or in no
        workspace when that membership is gone or the workspace is suspended - or None when there is no such
        session, or it is revoked or expired.

        One round-trip, by join. `users.actor_id` is a PK/FK onto `actors` - the two ids are the
        *same value* - so `auth_sessions.user_id` joins straight to `actors.id` with no hop
        through `users`; the membership in the session's active workspace and that workspace's standing ride
        the same query. This runs on every authenticated request; a second query here
        would be a second query on the hottest path in the product.

        At the token's door this answers ONE question: is the credential itself real and still
        live? Whether the ACCOUNT may act is a different question with one answer-place -
        `deps.authenticate` (admin_ops law 3). It used to answer both, and answering both is how
        a suspended person was told "invalid or expired credentials": a 401 that sends someone
        to the password-reset form over an account that was switched off on purpose. The door's
        `Forbidden("account suspended")` is the true sentence, and it could never fire while
        this line quietly returned None first.

        A `proof` (`live_principal`) has no door after it and no sentence to say: it is a yes or
        a no, so a suspended actor, or a session with no workspace to act in, proves nothing.
        """
        row = (
            await session.execute(
                select(cls, Actor, WorkspaceMember, Workspace.id)
                .join(Actor, Actor.id == cls.user_id)
                .outerjoin(
                    WorkspaceMember,
                    (WorkspaceMember.workspace_id == cls.active_workspace_id)
                    & (WorkspaceMember.actor_id == cls.user_id),
                )
                .outerjoin(
                    Workspace,
                    (Workspace.id == WorkspaceMember.workspace_id)
                    & (Workspace.status == WorkspaceStatus.ACTIVE.value),
                )
                .where(where)
            )
        ).first()
        if row is None:
            return None
        auth_session, actor, member, live_workspace_id = row  # Row unpacks directly
        if not auth_session.is_live():
            return None
        if live_workspace_id is None:
            member = None  # the membership is gone, or the workspace is suspended
        if proof and (member is None or not actor.is_active()):
            return None
        return auth_session, Principal(
            actor=actor,
            workspace_id=None if member is None else member.workspace_id,
            role=None if member is None else member.role,
            auth_session_id=auth_session.id,
        )

    @classmethod
    def issue(cls, user_id: uuid.UUID, user_agent: str = "") -> tuple[AuthSession, str]:
        """Mint a session. Returns the row and **the plaintext token, which exists only here**.

        The caller hands the plaintext to the client and forgets it. What is stored is the hash.
        There is no code path that can recover a token from the database - which is the property
        that makes a database dump survivable.
        """
        raw = new_session_token()
        row = cls(
            token_hash=hash_token(raw),
            user_id=user_id,
            expires_at=utcnow() + dt.timedelta(days=settings.auth.session_ttl_days),
            user_agent=user_agent[:400],
        )
        return row, raw

    # ── the operator's queries (admin surface) ────────────────────────────────────────────────

    @classmethod
    async def admin_list_for(
        cls, session: AsyncSession, user_id: uuid.UUID, *, limit: int = 50
    ) -> list[AuthSession]:
        """One user's sessions, newest first - METADATA rows for the console's Access tab.
        The serializer never carries `token_hash`; what leaves this list is when/where/alive."""
        return list(
            await session.scalars(
                select(cls)
                .where(cls.user_id == user_id)
                .order_by(cls.created_at.desc(), cls.id.desc())
                .limit(limit)
            )
        )

    @classmethod
    async def revoke_one(cls, session: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID) -> bool:
        """Revoke a single session BY ID, scoped to its owner - the admin Access tab's surgical
        sibling of `revoke_all_for`. The user_id scope means a mistyped id can never revoke
        someone else's session."""
        row = await session.scalar(select(cls).where(cls.id == session_id, cls.user_id == user_id))
        if row is None or row.revoked_at is not None:
            return False
        row.revoked_at = utcnow()
        await _forget_push_devices(session, [row.id])
        return True

    @classmethod
    async def latest_created_at(cls, session: AsyncSession, user_id: uuid.UUID) -> dt.datetime | None:
        """When this user last signed in (their newest session's birth) - the honest half of
        "last active": sessions record creation, not use; API keys carry `last_used_at`."""
        from sqlalchemy import func

        return await session.scalar(select(func.max(cls.created_at)).where(cls.user_id == user_id))

    @classmethod
    async def revoke(cls, session: AsyncSession, raw: str) -> bool:
        """Logout. The kill switch that a JWT cannot have."""
        row = await session.scalar(select(cls).where(cls.token_hash == hash_token(raw)))
        if row is None or row.revoked_at is not None:
            return False
        row.revoked_at = utcnow()
        await _forget_push_devices(session, [row.id])
        return True

    @classmethod
    async def live_count_for(cls, session: AsyncSession, user_id: uuid.UUID) -> int:
        """How many sessions could authenticate right now - the admin user-detail's number."""
        from sqlalchemy import func

        return int(
            await session.scalar(select(func.count()).where(cls.user_id == user_id, cls.live_clause())) or 0
        )

    @classmethod
    async def revoke_all_for(cls, session: AsyncSession, user_id: uuid.UUID) -> int:
        """Every live session for this user, revoked at once - the door a password reset must
        close behind itself: whoever knew the old password may be holding one open
        right now. Returns the count, for the audit line."""
        result = await session.execute(
            update(cls)
            .where(cls.user_id == user_id, cls.live_clause())
            .values(revoked_at=utcnow())
            .returning(cls.id)
        )
        revoked = list(result.scalars())
        await _forget_push_devices(session, revoked)
        return len(revoked)


async def _forget_push_devices(session: AsyncSession, session_ids: list[uuid.UUID]) -> None:
    """Delete the push devices these ended sessions registered, in the caller's transaction."""
    if session_ids:
        await session.execute(delete(_PUSH_DEVICES).where(_PUSH_DEVICES.c.session_id.in_(session_ids)))


async def _persist_rebind(auth_session_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    """Write a rebind in its OWN short transaction, `ApiKey.resolve`'s use-bump shape: the
    request session is rolled back on a read-only route, and the rebind must stick."""
    from prismoid.db.session import session_scope

    async with session_scope() as own:
        await own.execute(
            update(AuthSession)
            .where(AuthSession.id == auth_session_id)
            .values(active_workspace_id=workspace_id)
        )
        await own.commit()
