# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Presence (ADR-062). Thin: auth → validate → one operation, no transaction.

The stream handlers announce join / here / leave themselves (`api/stream.py`); this route is
the client's one voice - a composer's typing frame, fanned out to the task's watchers.
"""

from __future__ import annotations

from uuid import UUID

from prismoid.api.deps import ActorDep, SessionDep
from prismoid.api.serializers.presence import PresenceIn
from prismoid.operations import presence as presence_ops


async def post_presence(task_id: UUID, body: PresenceIn, actor: ActorDep, session: SessionDep) -> None:
    await presence_ops.typing(session, actor, task_id, conn=body.conn, on=body.typing, mode=body.mode)
