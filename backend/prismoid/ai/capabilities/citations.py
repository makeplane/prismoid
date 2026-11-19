# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The citation check: a citing run's answer names only the sources its task's reads were given.

An agent that cites (`AgentDef.cites`, this check in its `capabilities`) marks a claim with the id of
the page it rests on (`[S3]`); the ids come from the task's reads, an earlier step's (`TaskDeps.sources`) or this run's
(`research.citable`), never from the model. An answer that cites an id no read was given is told which,
as a `ModelRetry` while the check may send it back (`output_checks.may_send_back`: its own budget, the
shared pool, the segment's ceiling); past that the answer stands and its publish refuses it
(`context/citations.render_citations`).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.output import OutputContext

from prismoid.ai.capabilities.output_checks import may_send_back
from prismoid.ai.context.citations import UnknownSource, unknown_sources
from prismoid.ai.deps import TaskDeps
from prismoid.ai.tools.research import citable
from prismoid.schemas.outputs import SubmittedDocument


@dataclass
class CitationCheck(AbstractCapability[TaskDeps]):
    """A text answer or a submitted document citing an unread source is sent back, while room remains."""

    id: str | None = "citations"
    _sent: int = field(default=0, init=False, repr=False)  # the answers this run sent back

    @classmethod
    def get_serialization_name(cls) -> str | None:
        return None  # an agent definition's; never spec-constructed

    async def for_run(self, ctx: RunContext[TaskDeps]) -> CitationCheck:
        return replace(self)

    async def after_output_process(
        self, ctx: RunContext[TaskDeps], *, output_context: OutputContext, output: Any
    ) -> Any:
        text = output.markdown if isinstance(output, SubmittedDocument) else output
        if ctx.partial_output or not isinstance(text, str) or not may_send_back(ctx, self._sent):
            return output
        sources = citable(ctx.deps.sources, ctx.messages)
        if unknown := unknown_sources(text, sources):
            self._sent += 1
            raise ModelRetry(str(UnknownSource(unknown, sources)))
        return output
