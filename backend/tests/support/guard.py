# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Test rigs for the run's hooks: a run guard whose per-run copies can be read back, and a hook that
hands the request on as a copy (what a redacting or trimming hook ahead of ours does), and a
browser answer with its page facts."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from pydantic_ai import RunContext, ToolReturn
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models import ModelRequestContext

from prismoid.ai.capabilities.loops import RunGuard
from prismoid.ai.deps import TaskDeps
from prismoid.ai.tools.browser import PageState

USERS = "admin.example.com/users"


def browser_page(
    page: str | None, *, where: str = USERS, epoch: int | None = 1, unmoved: bool = False
) -> ToolReturn[str]:
    """A browser answer as the browser tool hands it on: text for the model, and its facts for the
    guard (`page` names the page brought back; None, an act that brought none)."""
    state = PageState(where=where, page=page, epoch=epoch if page else None, unmoved=unmoved)
    return ToolReturn(f"ok · {where}\n{page or ''}".rstrip(), metadata=state)


def watched(*, can_park: bool = False, outcome: bool = True) -> tuple[RunGuard, list[RunGuard]]:
    """A guard to mount, and the copies its runs took (`for_run`): what a run came to is read off its copy."""
    runs: list[RunGuard] = []

    class Watched(RunGuard):
        async def for_run(self, ctx: RunContext[TaskDeps]) -> RunGuard:
            run = await super().for_run(ctx)
            runs.append(run)
            return run

    return Watched(can_park=can_park, outcome=outcome), runs


@dataclass
class CopiesTheRequest(AbstractCapability[TaskDeps]):
    """A hook that sends the request on as a copy of the one the history holds."""

    @classmethod
    def get_serialization_name(cls) -> str | None:
        return None

    async def before_model_request(
        self, ctx: RunContext[TaskDeps], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        *head, last = request_context.messages
        request_context.messages = [*head, dataclasses.replace(last)]
        return request_context
