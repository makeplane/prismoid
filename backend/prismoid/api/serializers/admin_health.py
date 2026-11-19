# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Admin - the structured health + alerts shapes (admin surface). `*Out` = response.

The machine-readable sibling of `/healthz/full`: same sources (the worker's beat row, the
catalog's doctor, the stuck predicate), structure instead of strings - a dashboard can light a
pill per queue, and `prismoid admin health` can exit nonzero on exactly the findings that matter.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class QueueVitalsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    enqueued: int
    running: int
    oldest_wait_s: int
    clogged: bool


class WorkerVitalsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    beat_age_s: int | None  # None = never beaten
    status: str  # ok | stale | never


class DoctorFindingOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    check: str
    ref: str
    detail: str


class AlertOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    severity: str  # warn | error
    count: int
    detail: str


class AdminHealthOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    migration: str
    worker: WorkerVitalsOut
    queues: list[QueueVitalsOut]
    version_ghosts: int
    stuck_tasks: int
    doctor: list[DoctorFindingOut]
    # The alert list rides along: it is computed from the SAME reading (admin_health.Vitals), and a
    # dashboard that fetched the two apart paid for the stuck scan and the doctor twice a tick.
    # `GET /admin/alerts` remains, for a pager that wants only what is shouting.
    alerts: list[AlertOut]
