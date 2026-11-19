# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The agent roster's response shape. `*Out` = response.

`AgentOut` is a straight re-export of the `AgentView` value - the boundary vocabulary says
`GET /v1/agents` returns **agents**, and it does.
"""

from __future__ import annotations

from prismoid.schemas.agents import AgentView

AgentOut = AgentView
