# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Repricing - a new price-book version that never rewrites a historical invoice.

`put_book` installs version **N+1** by copying the version in force forward and applying the overrides
on top, so you reprice one model by naming one model - the rest of the book rides along unchanged. It
does **not** touch a single `usage_events` row: each already recorded the `price_version` that priced
it, so March's invoice stays priced by March's book after April's renegotiation. That is what
a *version* buys - the ledger is immutable, and the price that touched a row is part of the row, not a
lookup that shifts under it. Repricing that rewrites history is not repricing; it is forgery.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from typing import TypedDict

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.price_book import PriceBookEntry
from prismoid.errors import ValidationFailed
from prismoid.schemas.usage import Unit

# {ref: {unit: usd_per_unit_as_str}} - the wire shape of a reprice, prices as strings so JSON's float
# never rounds the cent the ledger law promises. The DB parses them to Decimal here, at the edge.
Overrides = dict[str, dict[str, str]]


class Book(TypedDict):
    version: int
    prices: dict[str, dict[str, str]]  # ref → {unit: usd_per_unit}


async def get_book(session: AsyncSession, version: int | None = None) -> Book:
    """The book at `version` (or the one in force), as `{version, prices: {ref: {unit: usd}}}`."""
    v = version if version is not None else await PriceBookEntry.current_version(session)
    book = await PriceBookEntry.current(session, v)
    prices = {f"{p}:{m}": {u.value: str(x) for u, x in units.items()} for (p, m), units in book.items()}
    return {"version": v, "prices": prices}


async def put_book(session: AsyncSession, overrides: Overrides) -> int:
    """Install version = current + 1 = (current book) with `overrides` applied. Returns the new version.

    Copy-forward, not replace: an override names only what changes, and everything else is carried at
    the same price into the new version, so a one-model reprice cannot silently drop the other models.
    """
    from prismoid.operations.models import invalidate_catalog_cache

    invalidate_catalog_cache()  # specs carry prices; a reprice clears the read cache
    if not overrides:
        raise ValidationFailed("a reprice must change at least one price", {"prices": "empty"})

    current_v = await PriceBookEntry.current_version(session)
    book = await PriceBookEntry.current(session, current_v)  # the base we copy forward

    for ref, units in overrides.items():
        provider, _, model = ref.partition(":")
        if not model:
            raise ValidationFailed(f"price ref must be provider:model_id, got {ref!r}", {"ref": ref})
        for unit, usd in units.items():
            try:
                parsed_unit = Unit(unit)  # an unknown unit is a typo, not a new billing dimension
                price = Decimal(str(usd))
            except (ValueError, InvalidOperation) as exc:
                raise ValidationFailed(f"bad price {ref} {unit}={usd!r}: {exc}", {"unit": unit}) from exc
            book.setdefault((provider, model), {})[parsed_unit] = price

    next_v = current_v + 1
    for (provider, model), units in book.items():
        for parsed_unit, price in units.items():
            session.add(
                PriceBookEntry(
                    id=uuid.uuid4(),
                    version=next_v,
                    provider=provider,
                    model=model,
                    unit=parsed_unit.value,
                    usd_per_unit=price,
                )
            )
    await session.commit()
    return next_v
