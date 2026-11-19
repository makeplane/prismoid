# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""project_invites - an owner-minted join link into a Project (ADR-062). A tenant root: the workspace is
stamped at flush, the project is referenced WITH its workspace, and only `hash_token(code)` is stored.
A link is live while unrevoked, unexpired and unused; `take_use` is the one gate.

Every link is single-use and time-bound ("Invites are always time
bound and one time use"): `mint` writes `max_uses = SINGLE_USE`, so the first accept that grants
membership spends it. The column stays (the cap it gates is still read from the row) - it was a
caller's choice of 1..500, default 25, before single-use."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Final, Literal

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Index, String, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, utcnow, uuid_pk
from prismoid.db.models import tenant
from prismoid.schemas.ids import hash_token, new_project_invite_code

type InviteState = Literal["live", "expired", "used_up", "revoked"]
type InviteRole = Literal["editor", "viewer"]  # never owner: an owner is made by transfer

SINGLE_USE: Final = 1  # every link lets one person in


class ProjectInvite(Base):
    __tablename__ = "project_invites"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "workspace_id"],
            ["projects.id", "projects.workspace_id"],
            name="fk_project_invites_project_workspace",
            ondelete="CASCADE",
        ),
        Index("ix_project_invites_project_created", "project_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # editor | viewer - never owner
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_uses: Mapped[int] = mapped_column(nullable=False)
    uses: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    created_by_actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("actors.id", ondelete="SET NULL"), default=None
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[dt.datetime] = created_at()

    def state(self, now: dt.datetime) -> InviteState:
        if self.revoked_at is not None:
            return "revoked"
        if self.uses >= self.max_uses:
            return "used_up"
        if self.expires_at <= now:
            return "expired"
        return "live"

    @classmethod
    async def mint(
        cls,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        role: InviteRole,
        expires_in_days: int,
        created_by: uuid.UUID,
    ) -> tuple[ProjectInvite, str]:
        raw = new_project_invite_code()
        row = cls(
            id=uuid.uuid4(),
            project_id=project_id,
            role=role,
            code_hash=hash_token(raw),
            expires_at=utcnow() + dt.timedelta(days=expires_in_days),
            max_uses=SINGLE_USE,
            created_by_actor_id=created_by,
        )
        session.add(row)
        await session.flush()
        return row, raw

    @classmethod
    async def by_code(
        cls, session: AsyncSession, raw: str, *, lock: bool = False, spent_ok: bool = False
    ) -> ProjectInvite | None:
        """Unfenced on purpose: the code IS the credential. The caller binds the row's workspace next.
        `lock` (the redeem path) holds the row `FOR UPDATE` to commit, so two redemptions of one
        link serialize: the second reads the first's committed membership and spends nothing.
        A live row, or with `spent_ok` a used one too (the redeem path answers the people already
        in with their role); revoked, expired and unknown codes are always None."""
        query = select(cls).where(cls.code_hash == hash_token(raw))
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        row = await session.scalar(query)
        if row is None or row.state(utcnow()) not in (("live", "used_up") if spent_ok else ("live",)):
            return None
        return row

    @classmethod
    async def take_use(cls, session: AsyncSession, invite_id: uuid.UUID) -> bool:
        """Spend the link's use: the conditional UPDATE is the single gate, so racing redeemers of
        one link get exactly one winner. Postgres now() is the clock. Fenced to the bound workspace
        (the redeemer binds the row's workspace first): a wrong-workspace bind spends nothing."""
        got = await session.scalar(
            update(cls)
            .where(
                cls.id == invite_id,
                cls.workspace_id == tenant.workspace_of(session),
                cls.revoked_at.is_(None),
                cls.expires_at > func.now(),
                cls.uses < cls.max_uses,
            )
            .values(uses=cls.uses + 1)
            .returning(cls.id)
        )
        return got is not None

    @classmethod
    async def for_project(
        cls, session: AsyncSession, project_id: uuid.UUID, *, limit: int = 200
    ) -> list[ProjectInvite]:
        return list(
            await session.scalars(
                select(cls)
                .where(cls.project_id == project_id, cls.workspace_id == tenant.workspace_of(session))
                .order_by(cls.created_at.desc(), cls.id.desc())
                .limit(limit)
            )
        )

    @classmethod
    async def get_in_project(
        cls, session: AsyncSession, project_id: uuid.UUID, invite_id: uuid.UUID
    ) -> ProjectInvite | None:
        return await session.scalar(
            select(cls).where(
                cls.id == invite_id,
                cls.project_id == project_id,
                cls.workspace_id == tenant.workspace_of(session),
            )
        )
