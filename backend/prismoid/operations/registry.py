# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The agent roster, composed for the API.

This file exists because of the law, and the law is right: **views never touch agents**.
`api/` cannot import `prismoid.ai` - `just layers` fails the build if it tries - so
`GET /v1/agents` cannot read `REGISTRY` directly. It asks an operation, and the operation hands
back **values** (`schemas`), never `AgentDef` objects (ORM rows and engine internals never
cross upward).

The payoff is not tidiness. It is that the engine stays extractable: the day `ai/` moves into its
own process, this file changes from an import into an RPC and **not one view notices.**

(This file is the composer for `api/endpoints/registry.py` and
`api/serializers/registry.py`.)
"""

from __future__ import annotations

from prismoid.ai.agents.all import roster
from prismoid.schemas.agents import AgentView


def list_agents() -> list[AgentView]:
    return [
        AgentView(
            name=str(a.name),
            capability=a.model_class,
            description=a.description,
            tools=list(a.tools),
            skills=[str(s) for s in a.skills],
            verifier_suite=a.verifier_suite,
            can_ask_user=a.can_ask_user,
        )
        for a in roster()
    ]
