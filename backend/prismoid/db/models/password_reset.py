# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""PasswordReset - a single-use, emailed reset code (hashed at rest, TTL-bound) that lets a
locked-out User set a new Credential; superseded by re-request, swept after a grace.

The `cn_` shape from ADR-020, applied to the front door: the plaintext `cr_…` exists in the
email and nowhere else - what is stored is a SHA-256, and the row keeps its timestamps
(`created_at` / `consumed_at` / `superseded_at`) as the durable trail of who asked and what
happened. The code is 32 bytes of `secrets`-grade randomness, so sha256 is the right hash for
exactly the reason `schemas/ids.hash_token` documents: there is no dictionary to attack.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import ForeignKey, String, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.conf import settings
from prismoid.db.base import Base, created_at, rows_affected, utcnow, uuid_pk
from prismoid.schemas.ids import hash_token, new_password_reset_code


class PasswordReset(Base):
    __tablename__ = "password_resets"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.actor_id", ondelete="CASCADE"), nullable=False, index=True
    )
    # SHA-256 of the emailed `cr_…` code. Nullable because a claim CLEARS it - the lookup key
    # cannot live inside the box it opens (the connectors.oauth_state_hash rule), and the unique
    # index is what makes the claim race-proof: two concurrent resets cannot both win.
    code_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, default=None)
    expires_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    consumed_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    superseded_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    created_at: Mapped[dt.datetime] = created_at()

    @classmethod
    def issue(cls, user_id: uuid.UUID) -> tuple[PasswordReset, str]:
        """Mint a code. Returns the row and **the plaintext, which exists only here and in the
        email the caller sends**. What is stored is the hash."""
        raw = new_password_reset_code()
        row = cls(
            user_id=user_id,
            code_hash=hash_token(raw),
            expires_at=utcnow() + dt.timedelta(minutes=settings.auth.password_reset_ttl_minutes),
        )
        return row, raw

    @classmethod
    async def consume(cls, session: AsyncSession, code_hash: str) -> PasswordReset | None:
        """Atomic single-use claim - hash match + TTL + clear, one UPDATE (the ADR-020 shape).

        Two racing calls cannot both win: the second sees `code_hash` already NULL and matches
        zero rows, exactly like a replay or an expired code. None means only "no live code",
        never which of the three reasons - the caller's error must not narrate.
        """
        claimed = (
            await session.execute(
                update(cls)
                .where(cls.code_hash == code_hash, cls.expires_at > utcnow())
                .values(code_hash=None, consumed_at=utcnow())
                .returning(cls.id)
            )
        ).scalar_one_or_none()
        if claimed is None:
            return None
        return await session.get(cls, claimed)

    @classmethod
    async def supersede_pending(cls, session: AsyncSession, user_id: uuid.UUID) -> int:
        """A new request retires every code still pending for this user - at most one key to the
        front door exists at any moment, and the retired rows keep their timestamps as record."""
        result = await session.execute(
            update(cls)
            .where(cls.user_id == user_id, cls.code_hash.is_not(None))
            .values(code_hash=None, superseded_at=utcnow())
        )
        return rows_affected(result)

    @classmethod
    async def recent_count(cls, session: AsyncSession, user_id: uuid.UUID, window: dt.timedelta) -> int:
        """Requests in the trailing window - the durable half of the throttle. Outbound mail an
        attacker can trigger for free is a nuisance cannon; this is its ammunition limit."""
        return (
            await session.scalar(
                select(func.count())
                .select_from(cls)
                .where(cls.user_id == user_id, cls.created_at >= utcnow() - window)
            )
            or 0
        )
