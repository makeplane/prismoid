# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The forced answer: the last request a step's ceiling allows is an answer, never one more tool call.

A run that meets its request ceiling (`Envelope`, pydantic-ai's `UsageLimits`) mid-loop dies with
nothing to show for its reads. On the request the ceiling leaves last, function tools are off
(`tool_choice="none"`, the framework's per-request setting: the tool list stays, so the cached prefix
holds) and one line tells the model to answer from what it has, with its confidence and what it could
not verify. Both read the run's own request count, so a replayed run decides the same.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.settings import ModelSettings

from prismoid.ai.deps import TaskDeps
from prismoid.ai.rules.budget import requests_left
from prismoid.schemas.guard import FORCED_ANSWER

if TYPE_CHECKING:
    from pydantic_ai.models import ModelRequestContext


def _last(ctx: RunContext[TaskDeps]) -> bool:
    """Whether the coming request is the last the segment's ceiling allows."""
    return requests_left(ctx) == 1


def _settings(ctx: RunContext[TaskDeps]) -> ModelSettings:
    return ModelSettings(tool_choice="none") if _last(ctx) else ModelSettings()


@dataclass
class ForcedAnswer(AbstractCapability[TaskDeps]):
    """At the ceiling's last request: the tools off, and the line that asks for the answer."""

    id: str | None = "forced_answer"

    @classmethod
    def get_serialization_name(cls) -> str | None:
        return None  # built per agent definition; never spec-constructed

    def get_model_settings(self) -> Callable[[RunContext[TaskDeps]], ModelSettings]:
        return _settings

    async def before_model_request(
        self, ctx: RunContext[TaskDeps], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        if _last(ctx):
            ctx.enqueue(UserPromptPart(FORCED_ANSWER), priority="asap")
        return request_context
