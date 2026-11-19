# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The safe points (ADR-064): where a running step takes a steer. One pydantic-ai capability.

* before each model request - the steers this run has not been told enter its conversation as
  attributed lines (enqueued `asap`: pydantic-ai puts them in this request and in the history);
* before each tool dispatch - a call the model chose before a steer arrived is HELD, never run
  (`SkipToolExecution`: its result is the reason, and no retry budget is spent); a paused task
  parks the run here, when the run can park (`CallDeferred`, marked PAUSE_METADATA) - except a
  question or a connect block, which park as themselves;
* when the run is about to end - the steer is enqueued and pydantic-ai's pending-message drain
  turns the ending into one more model request; the run's stream is told (`on_redirect`), so the
  revised answer streams as a new message, never glued to the one it replaces.

A steer by anyone but the task's creator borrows her connectors for the rest of the run
(`Steering.borrows`: every call asks her first), set at whichever of these points tells the run,
before the model can act on it.

A tool already running is never cut; cancel is the only hard stop. The inbox is the truth: a
safe point that cannot read it is skipped (logged), a record that cannot be written is logged and
the run goes on, and the fences in operations keep the invariant. Steering never fails a run. A
task that ended under the run (a failed wave's siblings, a cancelled task's step) stops at the next
safe point, not at its own last word: a DBOS cancel lands only at the next durable step, and a run
has none.

Each inbox read is a logged operation of the step's call log (`inbox.pending`, ADR-089): a resumed
attempt reads what the dead one read at the same safe point, a failed read included, and only a
safe point past the last logged one reads the live inbox."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import KW_ONLY, dataclass, field
from typing import TYPE_CHECKING, Any

import logfire
from pydantic_ai import DeferredToolRequests, RunContext
from pydantic_ai.capabilities import AbstractCapability, durable_operation
from pydantic_ai.exceptions import CallDeferred, SkipToolExecution
from pydantic_ai.messages import ToolCallPart, UserPromptPart
from pydantic_ai.tools import ToolDefinition
from pydantic_graph import End

from prismoid.ai.deps import SteerInbox, TaskDeps
from prismoid.ai.tools import catalog
from prismoid.ai.tools.ask import ASK_USER, REQUEST_CONNECTOR
from prismoid.ai.tools.call_rows import Said, say
from prismoid.errors import ModelIncapable
from prismoid.schemas.steering import (
    PAUSE_METADATA,
    SafePoint,
    SteerNote,
    SteerPending,
    final_text,
    held_reason,
    held_text,
    injection_text,
)

if TYPE_CHECKING:
    from pydantic_ai.capabilities.abstract import AgentNode, NodeResult
    from pydantic_ai.models import ModelRequestContext

# The deferrals that park as themselves: a pause never takes their place (the gate they open is
# the park, and a steer still holds them like any call).
_OWN_PARK = frozenset({ASK_USER, REQUEST_CONNECTOR})


async def _record(write: Awaitable[None], what: str) -> None:
    """A steering record that fails is logged and dropped - it never fails the run."""
    try:
        await write
    except Exception:
        logfire.exception("a steering safe point could not record {what}; the run goes on", what=what)


@dataclass(frozen=True)
class _InboxRead:
    """One logged inbox read: what it said (`None`: the read failed) and the pause switch it left."""

    pending: SteerPending | None
    park_pause: bool


@dataclass
class Steering(AbstractCapability[TaskDeps]):
    inbox: SteerInbox
    can_park: bool = False  # the agent's output type admits a deferral, and operations allows it
    # The run's stream, told when a final answer is redirected (`build()` hands it `TapSlot.restart`).
    on_redirect: Callable[[], Awaitable[None]] | None = None
    _: KW_ONLY
    id: str | None = "steering"  # pinned: the call log names its reads by it
    # This run's state: a teammate's steer has reached the model, so every connector call asks the creator.
    borrowed: bool = field(default=False, init=False)

    @classmethod
    def get_serialization_name(cls) -> str | None:
        return None  # holds a callable inbox; never spec-constructed

    def borrows(self) -> bool:
        """Whether a teammate's steer has reached this run: the connector rules' getter."""
        return self.borrowed

    def _borrow(self, steers: Sequence[SteerNote]) -> None:
        """These steers are about to reach the model: a teammate's among them borrows the creator's
        connectors from here on - set first, so nothing the model does after it runs on her dial."""
        if not all(s.by_creator for s in steers):
            self.borrowed = True

    async def _pending(self, ctx: RunContext[TaskDeps]) -> SteerPending | None:
        """The safe point's read, and the stop when the task has ended under the run."""
        read = await self._read(ctx, self.inbox.told)
        self.inbox.park_pause = read.park_pause  # a replayed read leaves the switch the live one left
        if read.pending is not None and read.pending.ended:
            raise ModelIncapable("the task ended while this step ran - stopped", infra=True)
        return read.pending

    @durable_operation(name="inbox.pending")
    async def _read(self, ctx: RunContext[TaskDeps], after_epoch: int) -> _InboxRead:
        """The inbox as of this safe point, logged; a read that failed is logged as one."""
        try:
            pending = await self.inbox.pending(after_epoch)
        except Exception:
            logfire.exception("a steering safe point could not read its inbox; skipped")
            pending = None
        return _InboxRead(pending, self.inbox.park_pause)

    async def before_model_request(
        self, ctx: RunContext[TaskDeps], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        pending = await self._pending(ctx)
        if pending is None or not pending.steers:
            return request_context
        self._borrow(pending.steers)
        frame = ctx.deps.frame
        told = injection_text(pending.steers)
        ctx.enqueue(UserPromptPart(frame.paths.render(told) if frame else told), priority="asap")
        self.inbox.told = pending.steers[-1].epoch
        await _record(self.inbox.applied(pending.steers, SafePoint.BEFORE_MODEL), "applied")
        return request_context

    async def before_tool_execute(
        self,
        ctx: RunContext[TaskDeps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        pending = await self._pending(ctx)
        if pending is None:
            return args
        if pending.paused and self.can_park and self.inbox.park_pause and call.tool_name not in _OWN_PARK:
            await _record(self.inbox.paused_here(SafePoint.BEFORE_TOOL), "paused_here")
            raise CallDeferred(metadata=dict(PAUSE_METADATA))
        if not pending.steers:
            return args
        self._borrow(pending.steers)
        await _record(self.inbox.applied(pending.steers, SafePoint.BEFORE_TOOL), "applied")
        reason = held_reason(pending.steers)
        held = Said(
            catalog.journal_name(call.tool_name), False, denied_reason=reason, tool_call_id=call.tool_call_id
        )
        await _record(say(ctx.deps.journal_tool, held), "journal")
        raise SkipToolExecution(held_text(pending.steers))  # `told` stays: the next request injects

    async def after_node_run(
        self, ctx: RunContext[TaskDeps], *, node: AgentNode[TaskDeps], result: NodeResult[TaskDeps]
    ) -> NodeResult[TaskDeps]:
        if not isinstance(result, End):
            return result
        if isinstance(result.data.output, DeferredToolRequests):
            return result  # a park (a question, a pause): the steer applies when it resumes
        pending = await self._pending(ctx)
        if pending is None or not pending.steers:
            return result
        self._borrow(pending.steers)
        ctx.enqueue(final_text(pending.steers), priority="asap")  # the drain redirects the ending
        self.inbox.told = pending.steers[-1].epoch
        if self.on_redirect is not None:
            await _record(self.on_redirect(), "the redirect")
        await _record(self.inbox.applied(pending.steers, SafePoint.FINAL_ANSWER), "applied")
        return result
