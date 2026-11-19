# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A seat's records name their pages by source id; the system fills each record's URL from the read.

A gather seat (`base.Mode` for the `gather` lane role) answers with a `RecordSet` whose records each say
the id of the page they rest on (`S3`), the ids the task's and the run's reads were given
(`research.citable`, the same set a citing answer is held to). Each record's `url` is that page, so the
model never re-types a URL. A record naming an id no read was given is sent back as a `ModelRetry` while
the check may send it back (`output_checks.may_send_back`: its own budget, the shared pool, the segment's
ceiling); past that the record is dropped, since a record whose page nobody read is not evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.output import OutputContext

from prismoid.ai.capabilities.output_checks import may_send_back
from prismoid.ai.context.citations import UnknownSource
from prismoid.ai.deps import TaskDeps
from prismoid.ai.tools.research import citable
from prismoid.schemas.swarm import RecordSet


def sourced(records: RecordSet, sources: dict[str, str]) -> tuple[RecordSet, list[str]]:
    """`records` with each URL filled from its source id, the records naming an unread id dropped, and those ids."""
    kept = [r.model_copy(update={"url": sources[r.source]}) for r in records.records if r.source in sources]
    unknown = list(dict.fromkeys(r.source or "(none)" for r in records.records if r.source not in sources))
    return records.model_copy(update={"records": kept}), unknown


@dataclass
class RecordSources(AbstractCapability[TaskDeps]):
    """A seat's records, each tied to the page its source id names."""

    id: str | None = "record_sources"
    _sent: int = field(default=0, init=False, repr=False)  # the record sets this run sent back

    @classmethod
    def get_serialization_name(cls) -> str | None:
        return None  # mounted per mode; never spec-constructed

    async def for_run(self, ctx: RunContext[TaskDeps]) -> RecordSources:
        return replace(self)

    async def after_output_process(
        self, ctx: RunContext[TaskDeps], *, output_context: OutputContext, output: Any
    ) -> Any:
        if ctx.partial_output or not isinstance(output, RecordSet):
            return output
        sources = citable(ctx.deps.sources, ctx.messages)
        found, unknown = sourced(output, sources)
        if unknown and may_send_back(ctx, self._sent):
            self._sent += 1
            raise ModelRetry(str(UnknownSource(unknown, sources)))
        return found
