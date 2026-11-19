# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The agent roster - one of the three screens, exposed.

Thin: auth → one operation → serialize. The view cannot reach `prismoid.ai` and does not
try; `operations/registry.py` composes it.
"""

from __future__ import annotations

from prismoid.api.deps import ActorDep
from prismoid.api.serializers.registry import AgentOut
from prismoid.operations import registry as ops


async def list_agents(actor: ActorDep) -> list[AgentOut]:
    return ops.list_agents()
