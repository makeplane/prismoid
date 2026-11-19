# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Admin - the audit trail, readable (admin surface). An audit log nobody reads is a diary."""

from __future__ import annotations

from prismoid.api.deps import AdminDep, PageDep, SessionDep
from prismoid.api.serializers.admin_audit import AdminActionOut, admin_action_out
from prismoid.api.serializers.common import PageOut
from prismoid.db.base import keyset_cursor
from prismoid.operations import admin_ops


async def list_audit(
    page: PageDep,
    actor: AdminDep,
    session: SessionDep,
    subject_type: str | None = None,
    subject_id: str | None = None,
) -> PageOut[AdminActionOut]:
    rows, users = await admin_ops.list_audit(
        session,
        subject_type=subject_type,
        subject_id=subject_id,
        limit=page.limit,
        cursor=page.cursor,
    )
    next_cursor = keyset_cursor(rows[-1]) if len(rows) == page.limit else None
    return PageOut(items=[admin_action_out(r, users) for r in rows], next_cursor=next_cursor)
