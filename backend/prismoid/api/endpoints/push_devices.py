# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Push devices - a phone registers for the person's inbox as push notifications (ADR-100). Thin: auth →
one operation → serialize, no transaction. A login session only: a device lives as long as the
session that registered it, and an API key has no session to outlive (`SessionActorDep`). The
workspace door applies as on every route but the way back in: a device is the person's, not a
workspace's, yet a person with no workspace has no inbox to push, and logout still ends their
phones with the session. A server without push answers both with 404 (`operations/push_devices`)."""

from __future__ import annotations

from uuid import UUID

from prismoid.api.deps import LoginSessionIdDep, SessionActorDep, SessionDep
from prismoid.api.serializers.push_devices import PushDeviceIn, PushDeviceOut, device_out
from prismoid.operations import push_devices as ops


async def register_device(
    body: PushDeviceIn, actor: SessionActorDep, auth_session_id: LoginSessionIdDep, session: SessionDep
) -> PushDeviceOut:
    return device_out(await ops.register(session, actor, auth_session_id, body.registration()))


async def delete_device(device_id: UUID, actor: SessionActorDep, session: SessionDep) -> None:
    await ops.forget(session, actor, device_id)
