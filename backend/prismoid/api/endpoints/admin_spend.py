# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Admin - the operator's spend views: summary, daily series, one user's series.

Reads ride `operations/usage.py` (aggregation lives there or in the rollup, nowhere else).
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import Query

from prismoid.api.deps import AdminDep, SessionDep
from prismoid.api.serializers.admin_spend import (
    SpendPointOut,
    SpendSummaryOut,
    spend_points_out,
    spend_summary_out,
)
from prismoid.operations import usage as usage_ops


async def spend_summary(
    actor: AdminDep,
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> SpendSummaryOut:
    return spend_summary_out(await usage_ops.admin_summary(session, days=days))


async def spend_daily(
    actor: AdminDep,
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    group: Literal["model", "user"] | None = None,
) -> list[SpendPointOut]:
    return spend_points_out(await usage_ops.admin_daily(session, days=days, group=group))


async def user_spend_daily(
    id: UUID,
    actor: AdminDep,
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> list[SpendPointOut]:
    return spend_points_out(await usage_ops.admin_daily(session, days=days, actor_id=id))
