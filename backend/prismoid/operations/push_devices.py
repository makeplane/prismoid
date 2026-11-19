# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Push devices - the phones a person's inbox pushes to (ADR-100). A device is registered by a login
session and ends with it (`AuthSession.revoke*` deletes it); a token FCM calls dead is pruned by the
push sender (`operations/push.py`)."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import fcm
from prismoid.db.models.actor import Actor
from prismoid.db.models.push_device import PushDevice
from prismoid.errors import NotFound
from prismoid.schemas.push import PushDeviceRegistration


async def register(
    session: AsyncSession, actor: Actor, session_id: uuid.UUID, phone: PushDeviceRegistration
) -> PushDevice:
    """Register this phone for this person and session, or move a known token to them. Commits."""
    _push_on()
    row = await PushDevice.register(
        session,
        actor_id=actor.id,
        session_id=session_id,
        token=phone.token,
        platform=phone.platform,
        app_id=phone.app_id,
        app_version=phone.app_version,
    )
    await session.commit()
    return row


async def forget(session: AsyncSession, actor: Actor, device_id: uuid.UUID) -> None:
    """Unregister one of this person's phones. Anyone else's is NotFound, never Forbidden. Commits."""
    _push_on()
    if not await PushDevice.forget(session, device_id, actor.id):
        raise NotFound("no such device")
    await session.commit()


def _push_on() -> None:
    """A server without push (no `PRISMOID_PUSH_CREDENTIALS_JSON`, the default) has nothing to
    send to a phone, so it keeps none: both routes answer 404, as a route
    that is not there would, and no token is stored. Checked per request, not at mount, so the API
    contract is the same on every server."""
    if not fcm.enabled():
        raise NotFound("push notifications are not available on this server")
