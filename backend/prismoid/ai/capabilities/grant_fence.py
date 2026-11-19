# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The device regrant (ADR-078): a folder grant revoked under the running step is gone before the next
tool runs. One pydantic-ai capability, mounted only on a Machine frame on the device.

It runs after the steering and the run guard, which may stop the call first. Unlike a steer it fails
the call, never skips it: the fence is what a tool runs under, so a push that failed must not let one
through."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition

from prismoid.ai.deps import TaskDeps


@dataclass
class GrantFence(AbstractCapability[TaskDeps]):
    """Before each tool dispatch, the device frame's grants are pushed as they are now (`regrant`)."""

    regrant: Callable[[], Awaitable[None]]

    @classmethod
    def get_serialization_name(cls) -> str | None:
        return None  # holds a callable; never spec-constructed

    async def before_tool_execute(
        self,
        ctx: RunContext[TaskDeps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        await self.regrant()
        return args
