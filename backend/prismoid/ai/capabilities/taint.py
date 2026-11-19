# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The run's taint: whose text its model has read (trusted < user < third_party), as a watermark
that only rises, and the per-run nonce every fence in the run wears.

Before each model request the watermark is raised to the most foreign part the request carries: a
person's prompt is `user` (a stranger's line to a bot is `third_party`), a tool's result is what the
tool catalog says it is (a page, a search, the browser, a connected service: `third_party`). It is read from the conversation, never stored, so a
replayed run, which replays the same conversation, rebuilds the same watermark, and a run that
continues a conversation starts from what it already read. A step fed by the task's other steps starts
at what they read (`TaskDeps.taint_floor`): their outputs reach its prompt as Prismoid's words, so the
watermark could not see them. A connector write chosen while the run is
`third_party` asks a person first (`tainted`, which `ai/agents/mounts.py` hands the connector door).

The fence nonce is derived from the run id (stable across a replay under the call log), so the
fences a replayed tool writes are the bytes it wrote the first time; it rides only tool results and
per-turn text, never the static prompt.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, WrapRunHandler
from pydantic_ai.messages import ModelMessage, UserPromptPart

from prismoid.ai.deps import TaskDeps
from prismoid.ai.tools.call_rows import never_ran, ran
from prismoid.ai.tools.catalog import taint_of
from prismoid.ai.tools.fence import fence_nonce
from prismoid.schemas.egress import Taint, joined

if TYPE_CHECKING:
    from pydantic_ai.models import ModelRequestContext
    from pydantic_ai.run import AgentRunResult

_RUN: ContextVar[RunTaint | None] = ContextVar("prismoid_run_taint", default=None)


def watermark(
    messages: Iterable[ModelMessage], *, outputs: frozenset[str] = frozenset(), prompt: Taint = Taint.USER
) -> Taint:
    """The most foreign text in `messages`: a prompt (whose words are `prompt`), or a tool result that
    ran (an output tool's receipt, named in `outputs`, is the framework's own words)."""
    seen: list[Taint] = []
    for message in messages:
        for part in message.parts:
            if isinstance(part, UserPromptPart):
                seen.append(prompt)
            elif ran(part) and not never_ran(part) and part.tool_name not in outputs:
                seen.append(taint_of(part.tool_name))
    return joined(*seen)


def speaker(deps: TaskDeps) -> Taint:
    """Whose words a run's prompt carries: a stranger's when a bot replies to anyone but its owner
    (the reply lane sets `fetch_anchor` exactly then), else the person on the task."""
    return Taint.THIRD_PARTY if deps.fetch_anchor is not None else Taint.USER


def fence_nonce_for(run_id: str) -> str:
    """The run's fence nonce: unguessable from outside the run, the same on its replay."""
    return hashlib.sha256(f"fence:{run_id}".encode()).hexdigest()[:12]


def tainted() -> bool:
    """Whether the running run has read a third party's text (False outside a run)."""
    run = _RUN.get()
    return run is not None and run.level is Taint.THIRD_PARTY


@dataclass
class RunTaint(AbstractCapability[TaskDeps]):
    """The watermark of one run, from its floor, raised before each model request and never lowered."""

    level: Taint = Taint.TRUSTED

    async def for_run(self, ctx: RunContext[TaskDeps]) -> RunTaint:
        return replace(self, level=ctx.deps.taint_floor)

    async def wrap_run(self, ctx: RunContext[TaskDeps], *, handler: WrapRunHandler) -> AgentRunResult[Any]:
        token = _RUN.set(self)
        try:
            with fence_nonce(fence_nonce_for(ctx.run_id or "")):
                return await handler()
        finally:
            _RUN.reset(token)

    async def before_model_request(
        self, ctx: RunContext[TaskDeps], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        outputs = frozenset(tool.name for tool in request_context.model_request_parameters.output_tools)
        read = watermark(request_context.messages, outputs=outputs, prompt=speaker(ctx.deps))
        self.level = joined(self.level, read)
        return request_context
