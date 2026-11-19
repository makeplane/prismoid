# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The money's response shapes. `*Out` = response.

`Decimal` all the way to the wire. A cost that becomes a float in serialization is a cost that no
longer reconciles, and it does so silently.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from prismoid.schemas.usage import CostSummary


class PersonSpendOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    actor_id: UUID
    name: str
    usd: Decimal


class TaskUsageOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_usd: Decimal
    calls: int
    by_family: dict[str, Decimal]
    by_step: dict[str, Decimal]
    units: dict[str, int]
    by_person: list[PersonSpendOut]


def task_usage_out(summary: CostSummary) -> TaskUsageOut:
    return TaskUsageOut(
        total_usd=summary.total_usd,
        calls=summary.calls,
        by_family={str(f): c for f, c in summary.by_family.items()},
        by_step={str(s): c for s, c in summary.by_step.items()},
        units={str(u): n for u, n in summary.units.items()},
        by_person=[PersonSpendOut(actor_id=p.actor_id, name=p.name, usd=p.usd) for p in summary.by_person],
    )
