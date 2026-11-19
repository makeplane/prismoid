# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Capture - soft intake. Runs nothing; promotion is what makes it work.

Every route requires an Actor - authentication is never a feature that arrives later.
"""

from __future__ import annotations

from uuid import UUID

from prismoid.api.deps import ActorDep, SessionDep
from prismoid.api.serializers.capture import (
    CaptureIn,
    CaptureOut,
    PromoteIn,
    capture_out,
)
from prismoid.api.serializers.tasks import TaskAcceptedOut, task_accepted
from prismoid.operations import capture as ops


async def create_capture(body: CaptureIn, actor: ActorDep, session: SessionDep) -> CaptureOut:
    cap = await ops.add(session, actor, text=body.text, url=body.url, project_id=body.project_id)
    return capture_out(cap)


async def list_captures(actor: ActorDep, session: SessionDep) -> list[CaptureOut]:
    return [capture_out(c) for c in await ops.list_captures(session, actor)]


async def promote_capture(
    capture_id: UUID, body: PromoteIn, actor: ActorDep, session: SessionDep
) -> TaskAcceptedOut:
    task = await ops.promote(session, actor, capture_id, goal=body.goal or None)
    return task_accepted(task)
