# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Admin - the bot population, observed (admin surface). Read-only v1: heads across every
owner and project; the detail (instructions included) is an audited read, like the task peek."""

from __future__ import annotations

from uuid import UUID

from prismoid.api.deps import AdminDep, PageDep, SessionDep
from prismoid.api.serializers.admin_bots import (
    AdminBotDetailOut,
    AdminBotOut,
    admin_bot_detail_out,
    admin_bot_out,
)
from prismoid.api.serializers.common import PageOut
from prismoid.operations import admin_ops


async def list_bots(
    page: PageDep,
    actor: AdminDep,
    session: SessionDep,
    status: str | None = None,
    q: str | None = None,
) -> PageOut[AdminBotOut]:
    rows = await admin_ops.list_bots_admin(session, status=status, q=q, limit=page.limit, cursor=page.cursor)
    next_cursor = rows[-1]["bot"].cursor() if len(rows) == page.limit else None
    return PageOut(items=[admin_bot_out(r) for r in rows], next_cursor=next_cursor)


async def get_bot(bot_id: UUID, actor: AdminDep, session: SessionDep) -> AdminBotDetailOut:
    return admin_bot_detail_out(await admin_ops.get_bot_admin(session, actor, bot_id))
