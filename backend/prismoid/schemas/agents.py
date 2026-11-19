# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The agent roster, as a value.

`AgentDef` (in `ai/agents/base.py`) is an engine internal - it carries a prompt, tool grants and a
family exclusion. `AgentView` is what crosses the boundary: the roster a user, a Planner, or a
frontend can read. Two types, on purpose (no bare dicts across boundaries, and no engine
internals either).

The prompt is deliberately **not** in the view. It is the agent's judgment, it is long, and it is
not a thing an API consumer needs - `prismoid skills pull` is how you read and edit that kind of
text.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.outputs import VerifierSuite


class AgentView(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str  # a role - "writer", "maker" - never a model
    capability: ModelClass
    description: str
    tools: list[str]
    skills: list[str]
    verifier_suite: VerifierSuite | None = None
    can_ask_user: bool = False
