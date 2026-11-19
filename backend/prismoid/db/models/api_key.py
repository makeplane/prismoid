# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""ApiKey - an Actor's revocable programmatic token (hashed), shown in plaintext exactly once.

Belongs to an **Actor**, not a User: agents and service accounts can hold keys without a
migration the day we want them to.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import ColumnElement, ForeignKey, String, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, rows_affected, utcnow, uuid_pk
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.workspace import Workspace, WorkspaceMember, WorkspaceStatus
from prismoid.schemas.ids import API_KEY_PREFIX, hash_token, new_api_key
from prismoid.schemas.principal import Principal

# `last_used_at` is attribution, not accounting - and attribution must never turn a read into a
# hot write. One bump per minute per key, at most.
LAST_USED_THROTTLE = dt.timedelta(minutes=1)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("actors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    last_used_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    created_at: Mapped[dt.datetime] = created_at()

    def is_live(self) -> bool:
        return self.revoked_at is None

    def needs_use_bump(self) -> bool:
        return self.last_used_at is None or utcnow() - self.last_used_at > LAST_USED_THROTTLE

    @classmethod
    async def resolve(cls, session: AsyncSession, raw: str) -> Principal[Actor] | None:
        """Raw `ck_…` → the Actor that holds it and the workspace the key was minted in, or None. The
        ONLY api-key fetch path by token (`live_principal` is its twin by id).

        A key belongs to ONE workspace and never rebinds: a key whose actor has left that
        workspace, or whose workspace is suspended, is a failed resolve (a 401) - the same one query joins
        the membership and the workspace's standing (`_principal_for`).

        Bumps `last_used_at` at most once a minute (`needs_use_bump`). The throttle is not a
        nicety: without it, every authenticated request becomes a write, and an UPDATE on the
        hottest row in the system turns attribution into a lock convoy.

        The bump runs in its **own short transaction**, not on `session` (audit finding): `session`
        is the request session, and `get_session` rolls it back on teardown - so a read-only route
        never persisted the bump, while a write route could accidentally flush it on its own commit.
        A separate `session_scope` writes just this one column and commits immediately, holding no
        lock across the caller's work.
        """
        if not raw.startswith(API_KEY_PREFIX):
            return None
        found = await cls._principal_for(session, cls.key_hash == hash_token(raw))
        if found is None:
            return None
        api_key, principal = found
        if api_key.needs_use_bump():
            from prismoid.db.session import session_scope

            async with session_scope() as own:
                await own.execute(update(cls).where(cls.id == api_key.id).values(last_used_at=utcnow()))
                await own.commit()
        return principal

    @classmethod
    async def live_principal(cls, session: AsyncSession, key_id: uuid.UUID) -> Principal[Actor] | None:
        """`resolve`'s twin by row id: the screen socket's proof holds the id its ticket
        named, never the token. The same query, checks and principal as `resolve`
        (`_principal_for`), as a proof - so a suspended actor is a None too. It never bumps
        `last_used_at`: a re-proof every few seconds is not a use, and must not become a write."""
        found = await cls._principal_for(session, cls.id == key_id, proof=True)
        return None if found is None else found[1]

    @classmethod
    async def _principal_for(
        cls, session: AsyncSession, where: ColumnElement[bool], *, proof: bool = False
    ) -> tuple[ApiKey, Principal[Actor]] | None:
        """The key and the principal it proves, in one query - or None when there is no such key,
        it is revoked, its actor's membership in the key's workspace is gone or the workspace is suspended.

        Credential liveness only, at the token's door: the account's standing is
        `deps.authenticate`'s question (see `AuthSession._principal_for` for the whole
        reasoning). A `proof` (`live_principal`) has no door after it to ask, and is a yes or a
        no, so it asks here: a suspended actor proves nothing."""
        row = (
            await session.execute(
                select(cls, Actor, WorkspaceMember, Workspace.id)
                .join(Actor, Actor.id == cls.actor_id)
                .outerjoin(
                    WorkspaceMember,
                    (WorkspaceMember.workspace_id == cls.workspace_id)
                    & (WorkspaceMember.actor_id == cls.actor_id),
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
        api_key, actor, member, live_workspace_id = row  # Row unpacks directly
        if not api_key.is_live() or member is None or live_workspace_id is None:
            return None
        if proof and not actor.is_active():
            return None
        return api_key, Principal(
            actor=actor, workspace_id=member.workspace_id, role=member.role, api_key_id=api_key.id
        )

    @classmethod
    def issue(cls, actor_id: uuid.UUID, name: str, *, workspace_id: uuid.UUID) -> tuple[ApiKey, str]:
        """Mint a key in `workspace_id`. The plaintext `ck_…` is returned once and never stored."""
        raw = new_api_key()
        return cls(
            key_hash=hash_token(raw), actor_id=actor_id, name=name[:200], workspace_id=workspace_id
        ), raw

    @classmethod
    async def list_for(cls, session: AsyncSession, actor_id: uuid.UUID) -> list[ApiKey]:
        """This actor's keys minted into the bound workspace - a key never moves workspaces."""
        return await cls._listed(
            session, cls.workspace_id == tenant.workspace_of(session), cls.actor_id == actor_id
        )

    @classmethod
    async def admin_list_for(cls, session: AsyncSession, actor_id: uuid.UUID) -> list[ApiKey]:
        """Every key this actor holds, in every workspace - the operator console's read (admin_ops
        law 1: the console observes the whole population, platform mode, no workspace fence)."""
        return await cls._listed(session, cls.actor_id == actor_id)

    @classmethod
    async def _listed(cls, session: AsyncSession, *where: ColumnElement[bool]) -> list[ApiKey]:
        return list(await session.scalars(select(cls).where(*where).order_by(cls.created_at.desc())))

    @classmethod
    async def latest_used_at(cls, session: AsyncSession, actor_id: uuid.UUID) -> dt.datetime | None:
        """The newest `last_used_at` across this actor's keys - the API half of "last active"
        (admin surface); sessions contribute their creation time."""
        from sqlalchemy import func

        return await session.scalar(select(func.max(cls.last_used_at)).where(cls.actor_id == actor_id))

    @classmethod
    async def revoke(cls, session: AsyncSession, actor_id: uuid.UUID, key_id: uuid.UUID) -> bool:
        """Scoped by actor: you cannot revoke a key that is not yours, and asking about one
        tells you nothing (it returns False, not 403 - existence is not leaked). In the bound workspace
        only, like the list it pairs with."""
        return await cls._revoked(
            session,
            cls.id == key_id,
            cls.workspace_id == tenant.workspace_of(session),
            cls.actor_id == actor_id,
        )

    @classmethod
    async def admin_revoke(cls, session: AsyncSession, actor_id: uuid.UUID, key_id: uuid.UUID) -> bool:
        """The operator's revoke - one of this actor's keys, whichever workspace it was minted in."""
        return await cls._revoked(session, cls.id == key_id, cls.actor_id == actor_id)

    @classmethod
    async def _revoked(cls, session: AsyncSession, *where: ColumnElement[bool]) -> bool:
        row = await session.scalar(select(cls).where(*where))
        if row is None or row.revoked_at is not None:
            return False
        row.revoked_at = utcnow()
        return True

    @classmethod
    async def live_count_for(cls, session: AsyncSession, actor_id: uuid.UUID) -> int:
        """How many keys could authenticate right now - the admin user-detail's number."""
        from sqlalchemy import func

        return int(
            await session.scalar(
                select(func.count()).where(cls.actor_id == actor_id, cls.revoked_at.is_(None))
            )
            or 0
        )

    @classmethod
    async def revoke_all_for(cls, session: AsyncSession, actor_id: uuid.UUID) -> int:
        """Every live key this actor holds, revoked at once - `AuthSession.revoke_all_for`'s
        sibling, for the same door: whoever knew the old password could have minted a
        `ck_` key while they were in, and a reset that leaves it working has not taken the
        account back. Returns the count, for the audit line."""
        result = await session.execute(
            update(cls).where(cls.actor_id == actor_id, cls.revoked_at.is_(None)).values(revoked_at=utcnow())
        )
        return rows_affected(result)
