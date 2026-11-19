# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Machine's two buttons (ADR-063). Thin: auth → one operation → serialize."""

from uuid import UUID

from prismoid.api.deps import ActorDep, SessionDep
from prismoid.api.serializers.machines import MachineOut, machine_out
from prismoid.operations import machines


async def sleep_machine(task_id: UUID, actor: ActorDep, session: SessionDep) -> MachineOut:
    return machine_out(await machines.sleep_now(session, actor, task_id))


async def reset_machine(task_id: UUID, actor: ActorDep, session: SessionDep) -> MachineOut:
    return machine_out(await machines.reset_now(session, actor, task_id))
