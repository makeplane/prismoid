# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Admin - the operator's spend shapes (admin surface). `*Out` = response.

`Decimal` all the way to the wire, exactly like `serializers/usage.py` - a cost that becomes a
float in serialization no longer reconciles, and it fails silently.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class SpendByModelOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    cost_usd: Decimal
    calls: int


class TopSpenderOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: UUID | None  # None = spend with no attributable human (system/orphaned rows)
    email: str
    name: str
    cost_usd: Decimal
    calls: int


class PriceVersionOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: int
    calls: int


class SpendSummaryOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    days: int
    total_usd: Decimal
    calls: int
    mtd_usd: Decimal
    today_usd: Decimal
    by_model: list[SpendByModelOut]
    top_spenders: list[TopSpenderOut]
    price_versions: list[PriceVersionOut]
    zero_cost_calls: int  # metered-at-$0 rows in the window - the price book's blind spot


class SpendPointOut(BaseModel):
    """One day bucket. `key` is null (ungrouped), a `provider:model` ref, or a user email."""

    model_config = ConfigDict(frozen=True)

    day: str  # YYYY-MM-DD
    key: str | None
    cost_usd: Decimal
    calls: int


def spend_summary_out(data: dict[str, Any]) -> SpendSummaryOut:
    return SpendSummaryOut(
        days=data["days"],
        total_usd=data["total_usd"],
        calls=data["calls"],
        mtd_usd=data["mtd_usd"],
        today_usd=data["today_usd"],
        by_model=[SpendByModelOut(**m) for m in data["by_model"]],
        top_spenders=[TopSpenderOut(**t) for t in data["top_spenders"]],
        price_versions=[PriceVersionOut(**p) for p in data["price_versions"]],
        zero_cost_calls=data["zero_cost_calls"],
    )


def spend_points_out(rows: list[dict[str, Any]]) -> list[SpendPointOut]:
    return [SpendPointOut(**r) for r in rows]
