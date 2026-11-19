# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Device grants - what a person lets a task on their own computer reach: folders and Docker.
Postgres holds them; the worker tells the helper at the frame's next bind.

Thin: auth → validate → one operation call → serialize. ≤20 lines each, no business
logic, no transaction.
"""

from __future__ import annotations

from uuid import UUID

from prismoid.api.deps import ActorDep, SessionDep
from prismoid.api.serializers.common import OkOut
from prismoid.api.serializers.device_grants import DeviceGrantIn, DeviceGrantOut, device_grant_out
from prismoid.operations import device_grants as ops


async def list_device_grants(
    actor: ActorDep, session: SessionDep, task_id: UUID | None = None
) -> list[DeviceGrantOut]:
    return [device_grant_out(r) for r in await ops.list_grants(session, actor, task_id)]


async def add_device_grant(body: DeviceGrantIn, actor: ActorDep, session: SessionDep) -> DeviceGrantOut:
    row = await ops.add_grant(session, actor, body.task_id, kind=body.kind, path=body.path, mode=body.mode)
    return device_grant_out(row)


async def revoke_device_grant(grant_id: UUID, actor: ActorDep, session: SessionDep) -> OkOut:
    await ops.revoke_grant(session, actor, grant_id)
    return OkOut(ok=True)
