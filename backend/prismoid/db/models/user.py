# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""User - the human satellite of an Actor: email, profile, preferences, GDPR lifecycle.

`users.actor_id` is **PK and FK at once** - a User *is* an Actor, seen from the side that logs
in. The two ids are the same value, which is why every owner/creator FK in the system can point
at `actors` while the API still presents owners and members as users.

The thing that logs in, has a profile, and appears in Settings.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, escape_like, keyset_before, keyset_cursor
from prismoid.db.models.actor import Actor, ActorKind, Scope


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # `admin_page`'s keyset: `(created_at DESC, actor_id DESC)`. `users` keys on `actor_id`,
        # not `id`, so this is the pair - and without it the console's first screen full-scanned
        # and sorted the whole population per page (a keyset needs its index).
        Index("ix_users_created_at_actor_id", "created_at", "actor_id"),
    )

    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("actors.id", ondelete="CASCADE"), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(1000), default=None)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    preferences: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)

    created_at: Mapped[dt.datetime] = created_at()
    # When the emailed `ev_` code proved this address; null until it does. Signup works without it.
    email_verified_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    # GDPR erasure: the User is soft-deleted and scrubbed; the Actor survives so that the events
    # and ledger rows that point at it stay referentially whole. Attribution outlives the profile.
    # The erasure path, when built, must also RETIRE the person's `scope=user` memories - a
    # soft-delete never fires memories.user_id's FK CASCADE (see ADR-011's operational notes).
    deleted_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    @staticmethod
    def normalize_email(email: str) -> str:
        """One definition, used by signup, login and add_member - three definitions is how
        `Ana@co.com` ends up unable to log in to the account `ana@co.com` created."""
        return email.strip().lower()

    @classmethod
    async def by_email(cls, session: AsyncSession, email: str) -> User | None:
        """The ONLY email lookup path. Normalised, so `Ana@co.com` finds `ana@co.com`."""
        return await session.scalar(
            select(cls).where(cls.email == cls.normalize_email(email), cls.deleted_at.is_(None))
        )

    @classmethod
    async def by_actor_ids(cls, session: AsyncSession, actor_ids: list[uuid.UUID]) -> dict[uuid.UUID, User]:
        """The human satellites for a set of actors, one IN-query - how a member list gets its
        names without an N+1 (the API presents members as users)."""
        if not actor_ids:
            return {}
        rows = await session.scalars(select(cls).where(cls.actor_id.in_(actor_ids)))
        return {u.actor_id: u for u in rows}

    @classmethod
    async def signup(
        cls, session: AsyncSession, *, email: str, name: str, password_hash: str, admin: bool = False
    ) -> User:
        """Create Actor(kind=human) + User + Credential as one unit.

        The three rows are meaningless apart: an Actor with no User cannot log in, a User with no
        Credential cannot authenticate. They are created together or not at all - the operation
        that calls this owns the transaction.

        `admin` grants the platform admin scope. Only two callers pass it: `prismoid up`'s local first
        run and the operator's `prismoid admin bootstrap` on the box. No web path does - an empty table
        is not a credential, and signup does not verify the address.
        """
        from prismoid.db.models.credential import Credential

        normalized = cls.normalize_email(email)
        actor = Actor(
            kind=ActorKind.HUMAN.value,
            display_name=name,
            scopes=[Scope.ADMIN.value] if admin else [],
        )
        session.add(actor)
        await session.flush()  # the Actor id is the User id - we need it before we can use it

        user = cls(actor_id=actor.id, email=normalized, name=name)
        session.add(user)
        # Flushed on its own, before the Credential that points at it. The unit of work sorts
        # INSERTs by table dependency, but `credentials.user_id → users.actor_id` is a foreign key
        # with **no `relationship()` behind it** (deliberately - a password hash must not be
        # reachable by walking a User), and without one the sort has no edge to order on. It
        # emitted the Credential first and Postgres, correctly, refused it. Three explicit
        # flushes, three rows that cannot exist apart.
        await session.flush()

        session.add(Credential(user_id=user.actor_id, password_hash=password_hash))
        await session.flush()
        return user

    @classmethod
    async def count(cls, session: AsyncSession) -> int:
        return await session.scalar(select(func.count()).select_from(cls)) or 0

    # ── the operator's queries (admin surface) ────────────────────────────────────────────────

    @classmethod
    async def admin_page(
        cls,
        session: AsyncSession,
        *,
        q: str | None = None,
        status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[tuple[User, Actor]]:
        """Every human, newest first, keyset on `(created_at, actor_id)` - `users` keys on
        `actor_id`, not `id`, so it carries its own cursor recipe (`admin_cursor`).

        Soft-deleted users are listed too: the operator's view is the whole population, GDPR
        tombstones included; the serializer says which is which. (`by_email` hides them, and
        should - that is the login path.)"""
        query = (
            select(cls, Actor)
            .join(Actor, Actor.id == cls.actor_id)
            .order_by(cls.created_at.desc(), cls.actor_id.desc())
            .limit(limit)
        )
        if q:
            # A pasted user/actor id resolves exactly; anything else is an email/name substring.
            try:
                query = query.where(cls.actor_id == uuid.UUID(q.strip()))
            except ValueError:
                needle = f"%{escape_like(q)}%"
                query = query.where(
                    or_(cls.email.ilike(needle, escape="\\"), cls.name.ilike(needle, escape="\\"))
                )
        if status:
            query = query.where(Actor.status == status)
        if cursor is not None:
            query = query.where(keyset_before(cls, cursor, ident="actor_id"))
        return [(u, a) for u, a in (await session.execute(query)).tuples().all()]

    def admin_cursor(self) -> str:
        return keyset_cursor(self, ident="actor_id")

    @classmethod
    async def with_actor(cls, session: AsyncSession, user_id: uuid.UUID) -> tuple[User, Actor] | None:
        """The (profile, principal) pair the admin surface acts on - or None."""
        row = (
            await session.execute(
                select(cls, Actor).join(Actor, Actor.id == cls.actor_id).where(cls.actor_id == user_id)
            )
        ).first()
        return None if row is None else (row[0], row[1])

    @classmethod
    async def lock_row(cls, session: AsyncSession, actor_id: uuid.UUID) -> None:
        """Take this user's row lock (`FOR UPDATE`, held to commit) - the serialization point
        for per-user invariants that span multiple statements. The password-reset mint uses it: without
        it, two interleaved forgot-password requests each see "nothing pending" under READ
        COMMITTED and both mint, leaving two simultaneously-live reset codes."""
        await session.execute(select(cls.actor_id).where(cls.actor_id == actor_id).with_for_update())
