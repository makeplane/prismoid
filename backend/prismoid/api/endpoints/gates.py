# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Gates - the three-action resolve (accept | decline | cancel), MCP-aligned.

Thin: auth → validate → one operation → serialize. The transaction and the *wake-up* belong
to `operations/gates.py` - a view that owned either could commit a resolution and then fail to wake
the workflow that was waiting for it, which is a task parked for ever on a question somebody has
already answered.
"""

from __future__ import annotations

from uuid import UUID

from prismoid.api.deps import ActorDep, SessionDep
from prismoid.api.serializers.gates import (
    GateOut,
    GateResolvedOut,
    GateResolveIn,
    gate_out,
    gate_resolved,
)
from prismoid.operations import gates as ops


async def list_gates(actor: ActorDep, session: SessionDep) -> list[GateOut]:
    # `open_gates` is the creator's own queue (`Gate.open_for` joins on the task's creator) -
    # every row here is the viewer's own task, so `always` survives; the serializer's filter
    # is the law for any surface that ever lists a gate to someone else.
    return [
        gate_out(g, viewer_id=actor.id, creator_actor_id=actor.id, machine_driver=driver)
        for g, driver in await ops.open_gates(session, actor)
    ]


async def get_gate(gate_id: UUID, actor: ActorDep, session: SessionDep) -> GateOut:
    # Anyone who can see the task reads its open question; `always` only its creator, the rule
    # for every surface that shows a gate to someone else.
    gate, task, driver = await ops.open_gate(session, actor, gate_id)
    return gate_out(gate, viewer_id=actor.id, creator_actor_id=task.creator_actor_id, machine_driver=driver)


async def resolve_gate(
    gate_id: UUID, body: GateResolveIn, actor: ActorDep, session: SessionDep
) -> GateResolvedOut:
    resolution = await ops.resolve(session, actor, gate_id, body.action, body.content)
    return gate_resolved(gate_id, resolution)
