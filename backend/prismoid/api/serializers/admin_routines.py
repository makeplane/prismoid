# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Admin - the standing-work population's wire shapes (ADR-048). Observation plus ONE verb
(the audited pause); everything else is the product surface's job."""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AdminRoutineOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    owner_email: str | None  # None ⇒ the owner's User row was erased; the Actor remains
    project_id: UUID | None
    trigger_kind: str
    status: str
    paused_reason: str | None
    pause_note: str | None
    keeper: str
    budget_usd_per_run: float
    next_fire_at: dt.datetime | None
    last_fired_at: dt.datetime | None
    runs_minted: int
    consecutive_failures: int
    created_at: dt.datetime


class AdminRoutinePauseIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    reason: str = Field(min_length=3, max_length=400)


def admin_routine_out(row: dict[str, Any]) -> AdminRoutineOut:
    routine = row["routine"]
    owner = row["owner"]
    template = routine.template or {}
    return AdminRoutineOut(
        id=routine.id,
        name=routine.name,
        owner_email=getattr(owner, "email", None),
        project_id=routine.project_id,
        trigger_kind=routine.trigger_kind,
        status=routine.status,
        paused_reason=routine.paused_reason,
        pause_note=routine.pause_note,
        keeper=str(template.get("bot") or "one"),
        budget_usd_per_run=float(routine.budget_usd_per_run),
        next_fire_at=routine.next_fire_at,
        last_fired_at=routine.last_fired_at,
        runs_minted=routine.runs_minted,
        consecutive_failures=routine.consecutive_failures,
        created_at=routine.created_at,
    )
