# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""PriceBook - versioned unit prices per provider/model/unit.

Repricing changes **new** `usage_events` only: every row records the `price_version` that
priced it, so an invoice from March is still the invoice from March after you renegotiate in
April. Repricing that rewrites history is not repricing, it is forgery.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import Index, Numeric, String, UniqueConstraint, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, uuid_pk
from prismoid.schemas.usage import Unit


class PriceBookEntry(Base):
    __tablename__ = "price_book"
    __table_args__ = (
        UniqueConstraint("version", "provider", "model", "unit", name="price_book_key"),
        Index("ix_price_book_version", "version"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    version: Mapped[int] = mapped_column(nullable=False, default=1)

    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)  # schemas.usage.Unit

    # USD per single unit. `--price input_token=0.20/M` on the CLI is 0.20 / 1_000_000 here, so
    # the arithmetic at write time is a multiply and nothing else. 12 decimal places because a
    # per-token price is genuinely that small.
    usd_per_unit: Mapped[Decimal] = mapped_column(Numeric(20, 12), nullable=False)

    created_at: Mapped[dt.datetime] = created_at()

    # The floor, not the current version. `current_version()` reads the book in force from the data
    # (the highest version written), so `prismoid admin prices put` bumps it by writing rows, not by
    # editing a constant and redeploying - repricing is data. VERSION is only the seed default.
    VERSION = 1

    @classmethod
    async def current_version(cls, session: AsyncSession) -> int:
        """The book in force: the highest version ever written. A `put` adds version N+1, and the very
        next `catalog()` reads it here - no deploy. Falls back to VERSION on an empty (pre-seed) book."""
        highest = await session.scalar(select(func.max(cls.version)))
        return int(highest) if highest is not None else cls.VERSION

    @classmethod
    async def current(
        cls, session: AsyncSession, version: int | None = None
    ) -> dict[tuple[str, str], dict[Unit, Decimal]]:
        """(provider, model) → {unit: usd_per_unit}. One query for the whole book at `version` (or the
        version in force). Ask for a past version and you get March's book, byte for byte, in April."""
        v = version if version is not None else await cls.current_version(session)
        rows = await session.scalars(select(cls).where(cls.version == v))
        book: dict[tuple[str, str], dict[Unit, Decimal]] = {}
        for row in rows:
            book.setdefault((row.provider, row.model), {})[Unit(row.unit)] = row.usd_per_unit
        return book
