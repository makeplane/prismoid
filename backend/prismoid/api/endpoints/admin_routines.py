# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Admin - standing work, observed and stoppable (ADR-048). List across every owner;
ONE verb: the audited pause. Resume stays the OWNER's act - an operator stops the machine,
the human restarts their own habit."""

from __future__ import annotations

from uuid import UUID

from prismoid.api.deps import AdminDep, PageDep, SessionDep
from prismoid.api.serializers.admin_routines import (
    AdminRoutineOut,
    AdminRoutinePauseIn,
    admin_routine_out,
)
from prismoid.api.serializers.common import OkOut, PageOut
from prismoid.operations import admin_ops


async def list_routines(
    page: PageDep,
    actor: AdminDep,
    session: SessionDep,
    status: str | None = None,
    q: str | None = None,
) -> PageOut[AdminRoutineOut]:
    rows = await admin_ops.list_routines_admin(
        session, status=status, q=q, limit=page.limit, cursor=page.cursor
    )
    next_cursor = rows[-1]["routine"].cursor() if len(rows) == page.limit else None
    return PageOut(items=[admin_routine_out(r) for r in rows], next_cursor=next_cursor)


async def pause_routine(
    routine_id: UUID, body: AdminRoutinePauseIn, actor: AdminDep, session: SessionDep
) -> OkOut:
    await admin_ops.pause_routine_admin(session, actor, routine_id, reason=body.reason)
    return OkOut()
