# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Verification - the signed report attached to an Artifact.

The signature covers the canonical JSON of the report. That is what makes
`UPDATE verifications SET report = …` *detectable*: re-verification recomputes the canonical
bytes and checks them against the stored signature, so a quiet edit in psql produces
`signature_valid: false` rather than a lie with a green check (the tamper test).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, uuid_pk


class Verification(Base):
    __tablename__ = "verifications"

    id: Mapped[uuid.UUID] = uuid_pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False, index=True
    )

    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    suite: Mapped[str] = mapped_column(String(32), nullable=False)

    # The cross-family half. Stored as columns, not buried in the report, because
    # "was this judged by a different family than produced it" is a question we must be able to
    # ask of the whole table at once - it is the property that makes verification worth anything.
    producer_family: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    verifier_family: Mapped[str | None] = mapped_column(String(32), default=None)

    report: Mapped[dict[str, Any]] = mapped_column(nullable=False)  # VerificationReport
    signature: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    public_key: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    algorithm: Mapped[str] = mapped_column(String(16), nullable=False, default="ed25519")

    created_at: Mapped[dt.datetime] = created_at()

    @classmethod
    async def latest_for(cls, session: AsyncSession, artifact_id: uuid.UUID) -> Verification | None:
        """The current report for an artifact.

        **Append-only, and read newest-first**. A retry verifies again, and the
        row it writes does not overwrite the row that failed - the history of *why it took three
        attempts* is the most interesting thing about a document that took three attempts, and an
        UPDATE would throw it away.
        """
        return await session.scalar(
            select(cls).where(cls.artifact_id == artifact_id).order_by(cls.created_at.desc()).limit(1)
        )

    @classmethod
    async def all_for(cls, session: AsyncSession, artifact_id: uuid.UUID) -> list[Verification]:
        """Every attempt, oldest first - the retry ladder, as it happened."""
        return list(
            await session.scalars(select(cls).where(cls.artifact_id == artifact_id).order_by(cls.created_at))
        )
