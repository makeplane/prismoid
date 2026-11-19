# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a gate is on a task whose Machine is the device: the leaf
`gates.py` and its readers ask, so neither imports the other's hub. It opens and closes nothing.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.gate import Gate
from prismoid.db.models.task import Task
from prismoid.errors import Conflict, Forbidden
from prismoid.operations.machines import driver_of
from prismoid.schemas.device import DEVICE_AWAY, DEVICE_DRIVER
from prismoid.schemas.plan import GateKind, GateSpec, TaskStatus
from prismoid.schemas.screen import ControlEnd

TAKE_CONTROL_INSTEAD = "Take control and hand back instead."  # the 409 on a person's CONTROL accept


def shown_driver(task: Task) -> str | None:
    """The Machine driver a client is told with a task's gate: `device` when the task's browser is
    a window on the user's own computer, where a CONTROL gate is ended with Done;
    None on a cloud Machine, whose driver is never named to a client."""
    return DEVICE_DRIVER if driver_of(task) == DEVICE_DRIVER else None


async def device_wait(session: AsyncSession, task: Task) -> str | None:
    """What a paused task waits for when it is its device: the open device-away gate's
    title ("Waiting for MacBook Pro"), else None. `prismoid tasks show` says it."""
    if task.state is not TaskStatus.PAUSED:
        return None
    for gate in await Gate.open_for_task(session, task.id):
        spec = GateSpec.model_validate(gate.spec)
        if spec.kind is GateKind.PAUSE and spec.reason.startswith(DEVICE_AWAY):
            return spec.title
    return None


def control_wait_s(task: Task) -> int:
    """How long a task's CONTROL gate waits for a person before it declines as "nobody came". On a
    cloud Machine `control_wait_s`, under the park hold (`Settings._control_wait_under_park_hold`).
    On a device the person signs in in a window on their own computer, so it waits as long as a
    device may be away (`PRISMOID_LOCAL_AWAY_LIMIT_H`), and the park hold never sleeps that Machine
    meanwhile (`schemas.machines.awaits_done`)."""
    if shown_driver(task) == DEVICE_DRIVER:
        return settings.local.away_limit_h * 3600
    return settings.computer_use.control_wait_s


def device_done(task: Task, gate: Gate, actor: Actor) -> dict[str, Any]:
    """A person's accept on an open CONTROL gate (the caller holds the task's row lock). On a task
    whose Machine is the device it is the creator's "Done": they did what
    the agent asked in the window on their own computer and hand it back. The content is what a
    lease's ending gives (`resolve_control`), so the parked call reads the same hand-back. A
    teammate's Done is refused - the window is not on their computer - and on a cloud Machine a
    person's accept is still the 409: the lease's ending answers it."""
    if driver_of(task) != DEVICE_DRIVER:
        raise Conflict(TAKE_CONTROL_INSTEAD)
    if actor.id != task.creator_actor_id:
        raise Forbidden("only the task's creator can say Done: the browser is on their computer")
    held = max(0, int((utcnow() - gate.created_at).total_seconds()))
    return {
        "outcome": "accepted",
        "holder": actor.display_name,
        "held_s": held,
        "reason": str(ControlEnd.RELEASED),
    }
