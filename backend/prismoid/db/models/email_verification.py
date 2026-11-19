# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""EmailVerification - a single-use, emailed code that proves a User owns their address (hashed at
rest, TTL-bound); superseded by a resend, swept after a grace.

The `password_resets` shape (ADR-031): the plaintext `ev_...` exists in the email and nowhere
else, and the row keeps its timestamps as the trail. A claim is bound to the user it was minted
for, so one person's code never verifies another.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import ForeignKey, String, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.conf import settings
from prismoid.db.base import Base, created_at, rows_affected, utcnow, uuid_pk
from prismoid.schemas.ids import hash_token, new_email_verification_code


class EmailVerification(Base):
    __tablename__ = "email_verifications"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.actor_id", ondelete="CASCADE"), nullable=False, index=True
    )
    # SHA-256 of the emailed code, cleared by a claim: the lookup key cannot live inside the box it
    # opens, and the unique index makes the claim race-proof.
    code_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, default=None)
    expires_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    consumed_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    superseded_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    created_at: Mapped[dt.datetime] = created_at()

    @classmethod
    def issue(cls, user_id: uuid.UUID) -> tuple[EmailVerification, str]:
        """Mint a code. Returns the row and **the plaintext, which exists only here and in the email
        the caller sends**."""
        raw = new_email_verification_code()
        row = cls(
            user_id=user_id,
            code_hash=hash_token(raw),
            expires_at=utcnow() + dt.timedelta(minutes=settings.auth.email_verification_ttl_minutes),
        )
        return row, raw

    @classmethod
    async def consume(cls, session: AsyncSession, code_hash: str, user_id: uuid.UUID) -> bool:
        """Atomic single-use claim: hash match + this user + TTL + clear, one UPDATE. False means only
        "no live code for you" - a replay, an expiry, another person's code, never which."""
        claimed = await session.execute(
            update(cls)
            .where(cls.code_hash == code_hash, cls.user_id == user_id, cls.expires_at > utcnow())
            .values(code_hash=None, consumed_at=utcnow())
            .returning(cls.id)
        )
        return claimed.scalar_one_or_none() is not None

    @classmethod
    async def supersede_pending(cls, session: AsyncSession, user_id: uuid.UUID) -> int:
        """A resend retires every code still pending for this user."""
        result = await session.execute(
            update(cls)
            .where(cls.user_id == user_id, cls.code_hash.is_not(None))
            .values(code_hash=None, superseded_at=utcnow())
        )
        return rows_affected(result)

    @classmethod
    async def recent_count(cls, session: AsyncSession, user_id: uuid.UUID, window: dt.timedelta) -> int:
        """Codes minted in the trailing window: the durable half of the resend throttle."""
        return (
            await session.scalar(
                select(func.count())
                .select_from(cls)
                .where(cls.user_id == user_id, cls.created_at >= utcnow() - window)
            )
            or 0
        )
