# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every tool call's record: one hook (`ToolRecorder.wrap_tool_execute`) around every call of every
toolset, MCP included, mounted outermost on every built agent so it sees each call's real outcome.

It writes one `tool.called` per call, with keys never values: its own row, named from the tool catalog
with the call's label, unless the code under the call said one under that name (`call_rows.say`: a
result-dependent label, a refusal returned as text, a steer's or the guard's hold); rows said under
another name (a browser widen or save) ride beside it. A deferred call writes nothing here, because its
gate pair is its record, and neither does a call the run's own stop cut off before it ran. A call the log already finished is replayed and never written twice.

It is also the one place OUTCOME UNKNOWN is decided: a call whose intent the call log holds without a
result started before an interruption and may have partly happened, so it is answered with
`OUTCOME_UNKNOWN` and never run again. That is a pure read of recorded rows, the same on every replay.
A read changes nothing, so a read the interruption cut off simply runs again.

And it is where a new generation (a fence, a fresh attempt) is kept from writing twice: a world or
record write an earlier generation of the step finished under the same `call_digest` (its name and
args, hashed) is answered with that recorded result, marked `ALREADY_DONE`, and not made again. The
frame's own verbs (its publish, its browser) act on the frame a generation stands on, so they run
again, as does a read. A connector's tool, and any name the catalog does not hold, always counts as
acting: a server that labels a write read-only never gets it repeated.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, WrapToolExecuteHandler
from pydantic_ai.exceptions import ApprovalRequired, CallDeferred, SkipToolExecution, ToolRetryError
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from pydantic_core import to_json

from prismoid.ai.capabilities import durability
from prismoid.ai.deps import TaskDeps
from prismoid.ai.tools import catalog, labels
from prismoid.ai.tools.call_rows import Said, journal_row, recording
from prismoid.errors import ModelIncapable
from prismoid.schemas.side_effects import SideEffect

OUTCOME_UNKNOWN: Final = (
    "OUTCOME UNKNOWN: this call started before an interruption and its result was never recorded. "
    "It may have partly happened. Check the current state before repeating it."
)
UNKNOWN_REASON: Final = "outcome unknown: interrupted before its result was recorded"
ALREADY_DONE: Final = (
    "ALREADY DONE: an earlier run of this step made this exact call and it completed, so it was not "
    "made again. Its recorded result:"
)
_RERUNS: Final = frozenset({SideEffect.READ, SideEffect.PURE})  # cut off, these run again
_ONCE: Final = frozenset({SideEffect.WORLD, SideEffect.RECORD})  # finished, these never run twice
_ON_FRAME: Final = frozenset(t.journal for space in ("frame", "browser") for t in catalog.family(space))


@dataclass
class ToolRecorder(AbstractCapability[TaskDeps]):
    """One `tool.called` per tool call, and OUTCOME UNKNOWN for a call the log saw start and never end."""

    async def wrap_tool_execute(
        self,
        ctx: RunContext[TaskDeps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        handler: WrapToolExecuteHandler,
    ) -> Any:
        state = durability.call_state(call.tool_call_id, approved=ctx.tool_call_approved)
        journal = catalog.journal_name(call.tool_name)
        effect = catalog.side_effect_of(journal) or SideEffect.WORLD  # unlisted: acting, whatever its hints
        if state == "unknown" and effect in _RERUNS:
            state = "live"
        if state == "live" and effect in _ONCE and journal not in _ON_FRAME:
            digest = durability.call_digest(call.tool_name, args)
            if (done := durability.earlier_result(call.tool_call_id, digest)) is not None:
                return f"{ALREADY_DONE}\n\n{done if isinstance(done, str) else to_json(done).decode()}"
        record = _Call(ctx, call, args, replayed=state == "recorded")
        if state == "told":
            return OUTCOME_UNKNOWN  # a later recorded request already carried it: it is journaled once
        if state == "unknown":
            await journal_row(ctx.deps.journal_tool, record.own(UNKNOWN_REASON))
            return OUTCOME_UNKNOWN
        with recording() as said:
            try:
                result = await handler(args)
            except (CallDeferred, ApprovalRequired):
                raise  # a deferral's record is its gate pair
            except ModelIncapable:
                raise  # the run stopped under the call (the task ended): no call outcome to record
            except SkipToolExecution as held:
                await record.write(said, str(held.result))
                raise
            except Exception as exc:
                cause = exc.__cause__ if isinstance(exc, ToolRetryError) and exc.__cause__ else exc
                await record.write(said, f"{type(cause).__name__}: {cause}")
                raise
            await record.write(said, None)
        return result


@dataclass(frozen=True, slots=True)
class _Call:
    """One tool call as the recorder writes it."""

    ctx: RunContext[TaskDeps]
    call: ToolCallPart
    args: dict[str, Any]
    replayed: bool  # the call log holds its result: it was journaled when it ran
    started: float = field(default_factory=time.perf_counter)

    async def write(self, said: list[Said], failure: str | None) -> None:
        """The call's rows: what the code under it said, then its own unless one was said under its
        name; none for a replay. A said row's missing summary, duration and call id are the call's."""
        if self.replayed:
            return
        own = self.own(failure)
        mine = [row for row in said if row.tool == own.tool]
        if not mine:
            rows = [*said, own]
        else:  # the call's own row stands where its first part was said
            later = {id(row) for row in mine[1:]}
            rows = [_one(mine) if row is mine[0] else row for row in said if id(row) not in later]
        for row in rows:
            filled = dataclasses.replace(
                row,
                summary=row.summary or own.summary,
                duration_ms=row.duration_ms or own.duration_ms,
                tool_call_id=row.tool_call_id or own.tool_call_id,
            )
            await journal_row(self.ctx.deps.journal_tool, filled)

    def own(self, failure: str | None) -> Said:
        """The row for a call nothing under it described: the catalog's name, the call's label."""
        return Said(
            tool=catalog.journal_name(self.call.tool_name),
            ok=failure is None,
            summary=labels.label_for(self.call.tool_name, self.args, self.ctx.deps.frame),
            duration_ms=int((time.perf_counter() - self.started) * 1000),
            denied_reason=failure or "",
            tool_call_id=self.call.tool_call_id,
        )


def _one(rows: list[Said]) -> Said:
    """Several rows said under the call's own name (a batch's parts) as its one row: ok when any
    part was, each summary and failure named once."""
    ok = any(row.ok for row in rows)
    return dataclasses.replace(
        rows[0],
        ok=ok,
        summary=", ".join(dict.fromkeys(row.summary for row in rows if row.summary)),
        denied_reason=""
        if ok
        else "; ".join(dict.fromkeys(r.denied_reason for r in rows if r.denied_reason)),
        ref=next((row.ref for row in rows if row.ref), None),
    )
