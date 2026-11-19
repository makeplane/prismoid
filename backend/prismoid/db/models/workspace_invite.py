# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""workspace_invites - an emailed, single-use, hashed, TTL'd invitation into a workspace (ADR-061).
The plaintext `oi_` code exists once, in the email; only `hash_token(code)` is stored. One live
invite per (workspace, email): minting again supersedes the older one."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.conf import settings
from prismoid.db.base import Base, created_at, utcnow, uuid_pk
from prismoid.db.models.workspace import WorkspaceRole
from prismoid.schemas.ids import hash_token, new_workspace_invite_code


def _expiry() -> dt.datetime:
    return utcnow() + dt.timedelta(days=settings.workspaces.invite_ttl_days)


class WorkspaceInvite(Base):
    __tablename__ = "workspace_invites"
    __table_args__ = (Index("ix_workspace_invites_workspace_email", "workspace_id", "email"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    invited_by_actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("actors.id", ondelete="SET NULL"), default=None
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_expiry)
    accepted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[dt.datetime] = created_at()

    @classmethod
    async def mint(
        cls,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        email: str,
        role: WorkspaceRole,
        invited_by: uuid.UUID | None,
    ) -> tuple[WorkspaceInvite, str]:
        email = email.strip().lower()
        await session.execute(
            update(cls)
            .where(
                cls.workspace_id == workspace_id,
                cls.email == email,
                cls.accepted_at.is_(None),
                cls.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow())
        )
        raw = new_workspace_invite_code()
        row = cls(
            workspace_id=workspace_id,
            email=email,
            role=role.value,
            code_hash=hash_token(raw),
            invited_by_actor_id=invited_by,
        )
        session.add(row)
        await session.flush()
        return row, raw

    @classmethod
    async def by_code(cls, session: AsyncSession, raw: str) -> WorkspaceInvite | None:
        row = await session.scalar(select(cls).where(cls.code_hash == hash_token(raw)))
        if (
            row is None
            or row.accepted_at is not None
            or row.revoked_at is not None
            or row.expires_at <= utcnow()
        ):
            return None
        return row

    @classmethod
    async def recent_count_by(cls, session: AsyncSession, actor_id: uuid.UUID, window: dt.timedelta) -> int:
        """Invites this actor minted in the trailing window, live or not: each one sent a mail."""
        return (
            await session.scalar(
                select(func.count())
                .select_from(cls)
                .where(cls.invited_by_actor_id == actor_id, cls.created_at >= utcnow() - window)
            )
            or 0
        )

    @classmethod
    async def live_for_workspace(
        cls, session: AsyncSession, workspace_id: uuid.UUID
    ) -> list[WorkspaceInvite]:
        return list(
            await session.scalars(
                select(cls)
                .where(
                    cls.workspace_id == workspace_id,
                    cls.accepted_at.is_(None),
                    cls.revoked_at.is_(None),
                    cls.expires_at > utcnow(),
                )
                .order_by(cls.created_at, cls.id)
            )
        )

    @classmethod
    async def get_live(
        cls, session: AsyncSession, workspace_id: uuid.UUID, invite_id: uuid.UUID
    ) -> WorkspaceInvite | None:
        return await session.scalar(
            select(cls).where(
                cls.id == invite_id,
                cls.workspace_id == workspace_id,
                cls.accepted_at.is_(None),
                cls.revoked_at.is_(None),
                cls.expires_at > utcnow(),
            )
        )

    @classmethod
    async def consume(cls, session: AsyncSession, invite_id: uuid.UUID) -> bool:
        """Spend the invite once: the conditional UPDATE is the single-use gate, so two
        racing accepts cannot both win. False = someone else already spent (or revoked) it."""
        got = await session.scalar(
            update(cls)
            .where(cls.id == invite_id, cls.accepted_at.is_(None), cls.revoked_at.is_(None))
            .values(accepted_at=utcnow())
            .returning(cls.id)
        )
        return got is not None

    @classmethod
    async def revoke(cls, session: AsyncSession, invite: WorkspaceInvite) -> None:
        invite.revoked_at = utcnow()
        await session.flush()

    @classmethod
    async def retire(cls, session: AsyncSession, invite_id: uuid.UUID) -> None:
        """Un-mint an invite whose email never left the building (the password-reset precedent)."""
        await session.execute(delete(cls).where(cls.id == invite_id))
