# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""PushDevice - a phone that gets a person's inbox as push notifications: its FCM registration token,
the login session that registered it, and what app build it runs (ADR-100).

A device lives exactly as long as its session: a logout, a revocation or a password reset deletes
it in the same transaction (`AuthSession.revoke*`), the session sweep's hard delete cascades, and a
push only ever reaches a device whose session is still live (`live_for`), so an expired session's
phone hears nothing even before the sweep. The token is not a credential to Prismoid: alone it lets
nobody act, and only the deployment's Firebase key can send to it. It is stored as FCM handed it
(a send needs it) and never logged.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable

from sqlalchemy import ForeignKey, String, delete, exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, StrEnumColumn, created_at, rows_affected, uuid_pk, vocabulary
from prismoid.db.models.session import AuthSession
from prismoid.schemas.push import PushDevicePlatform


class PushDevice(Base):
    __tablename__ = "push_devices"
    __table_args__ = (vocabulary("platform", PushDevicePlatform),)

    id: Mapped[uuid.UUID] = uuid_pk()
    actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("actors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("auth_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    platform: Mapped[PushDevicePlatform] = mapped_column(
        StrEnumColumn(PushDevicePlatform, 16), nullable=False
    )
    app_id: Mapped[str] = mapped_column(String(200), nullable=False)
    app_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = created_at()
    last_seen_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    @classmethod
    async def register(
        cls,
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        session_id: uuid.UUID,
        token: str,
        platform: PushDevicePlatform,
        app_id: str,
        app_version: str,
    ) -> PushDevice:
        """Upsert on the token: a token already known (the same phone signing in again, or another
        person signing in on it) moves to this person and session, and is seen now. One statement,
        so two registrations of one token race onto one row."""
        moved = {
            "actor_id": actor_id,
            "session_id": session_id,
            "platform": platform,
            "app_id": app_id,
            "app_version": app_version,
            "last_seen_at": func.now(),
        }
        stmt = (
            insert(cls)
            .values(id=uuid.uuid4(), token=token, **moved)
            .on_conflict_do_update(index_elements=[cls.token], set_=moved)
            .returning(cls)
        )
        return (await session.scalars(stmt, execution_options={"populate_existing": True})).one()

    @classmethod
    async def forget(cls, session: AsyncSession, device_id: uuid.UUID, actor_id: uuid.UUID) -> bool:
        """Delete one of this person's devices; False when there is no such device of theirs."""
        result = await session.execute(delete(cls).where(cls.id == device_id, cls.actor_id == actor_id))
        return rows_affected(result) > 0

    @classmethod
    async def forget_ids(cls, session: AsyncSession, device_ids: Iterable[uuid.UUID]) -> int:
        """Delete devices by id: the push sender's prune of tokens FCM called dead."""
        ids = list(device_ids)
        if not ids:
            return 0
        return rows_affected(await session.execute(delete(cls).where(cls.id.in_(ids))))

    @classmethod
    async def live_for(cls, session: AsyncSession, actor_id: uuid.UUID) -> list[PushDevice]:
        """This person's devices whose session can still authenticate: the push's audience."""
        return list(
            await session.scalars(
                select(cls)
                .join(AuthSession, AuthSession.id == cls.session_id)
                .where(cls.actor_id == actor_id, AuthSession.live_clause())
                .order_by(cls.created_at)
            )
        )

    @classmethod
    async def any_live_for(cls, session: AsyncSession, actor_id: uuid.UUID) -> bool:
        """Has this person a phone to push to? The inbox asks before it enqueues a push at all."""
        live = (
            select(cls.id)
            .join(AuthSession, AuthSession.id == cls.session_id)
            .where(cls.actor_id == actor_id, AuthSession.live_clause())
        )
        return bool(await session.scalar(select(exists(live))))
