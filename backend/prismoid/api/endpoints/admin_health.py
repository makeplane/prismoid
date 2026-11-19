# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Admin - structured health + alerts, and the catalog's doctor over HTTP (admin surface).

`/healthz/full` answers "is the machine on" in strings; these answer "what needs a hand" in
structure. Same sources - the worker's beat row, the stuck predicate, `models.doctor()` - one
implementation each, shaped for a dashboard and for `prismoid admin health`'s exit code.
"""

from __future__ import annotations

from prismoid.api.deps import AdminDep, SessionDep
from prismoid.api.serializers.admin_health import AdminHealthOut, AlertOut, DoctorFindingOut
from prismoid.operations import admin_health as health_ops
from prismoid.operations import models as model_ops


async def get_health(actor: AdminDep, session: SessionDep) -> AdminHealthOut:
    return AdminHealthOut(**await health_ops.admin_health(session))


async def get_alerts(actor: AdminDep, session: SessionDep) -> list[AlertOut]:
    return [AlertOut(**a) for a in await health_ops.alerts(session)]


async def get_doctor(actor: AdminDep, session: SessionDep) -> list[DoctorFindingOut]:
    return [DoctorFindingOut(**f) for f in await model_ops.doctor(session)]
