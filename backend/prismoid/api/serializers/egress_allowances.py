# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Egress allowance shapes. `*In` = request, `*Out` = response."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field

from prismoid.db.models.egress_allowance import EgressAllowance


class EgressAllowanceCreateIn(BaseModel):
    host: str = Field(min_length=1, max_length=253)


class EgressAllowanceOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    host: str
    source: str
    gate_id: uuid.UUID | None
    created_at: dt.datetime
    revoked_at: dt.datetime | None


def egress_allowance_out(row: EgressAllowance) -> EgressAllowanceOut:
    return EgressAllowanceOut(
        id=row.id,
        host=row.host,
        source=row.source,
        gate_id=row.gate_id,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )
