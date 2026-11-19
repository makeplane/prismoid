# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Lane-2 message tap: what an agent's run streams live, as it is generated - its
visible text, its tool activity, and the drafts of what it is writing. `AgentRunner.run` hands one tap to
pydantic-ai's event stream per run; the tap turns events into wire frames and emits them through
the run's deps. It holds no registry and builds no agent, so it lives apart from `base.py`."""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import AsyncIterable, Callable
from typing import Any, Final

from pydantic_ai import RunContext
from pydantic_ai.messages import (
    AgentStreamEvent,
    FinalResultEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolCallPartDelta,
)

from prismoid.ai.agents.draft import DraftScanner
from prismoid.ai.deps import TaskDeps
from prismoid.ai.tools import catalog, labels
from prismoid.schemas.conversation import ChatActivity, ChatDelta, ChatEnd, ChatStart
from prismoid.schemas.events import (
    Activity,
    ArtifactDelta,
    MessageDelta,
    MessageEnd,
    MessageStart,
)
from prismoid.schemas.outputs import SUBMIT_DOCUMENT

# Message-frame pacing (≤10 frames/s per channel, ≤4 KB per frame). One slice per flush,
# 1200 chars - worst-case UTF-8 keeps frame+envelope under NOTIFY's 4 KB ceiling, and 0.12 s
# between flushes keeps the channel at ≤8 frames/s with a margin for the step's other frames.
_MESSAGE_FLUSH_S: Final = 0.12
MESSAGE_SLICE_CHARS: Final = 1200


class _DraftStream:
    """One draft-producing call's streaming state: the scanner plus emit bookkeeping.

    `raw=True` is the publishes-output lane: the deliverable streams VERBATIM -
    a typed collection as the output tool's JSON args, a document as the model's own text -
    and the flat-object scanner (which would fail closed on nested JSON) never runs. `path`
    names the draft on the wire; None falls back to what the scanner saw."""

    __slots__ = ("buffer", "last_flush", "path", "raw", "scanner", "sent")

    def __init__(
        self, *, raw: bool = False, path: str | None = None, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.scanner = DraftScanner(clock=clock)
        self.raw = raw
        self.path = path
        self.buffer = ""
        self.sent = 0
        self.last_flush = 0.0


class MessageTap:
    """The Lane-2 message stream: the agent's **visible text**, as it is generated.

    pydantic-ai calls the handler once per graph NODE - the model-request node AND the
    tool-execution node - and one tap instance spans the whole run, so text before and after
    tool calls accumulates into ONE message whose parts read as paragraphs.
    The agent's *output* is a tool call (typed), so what streams here is the model's prose -
    its narration-as-it-works - and the durable terminal is `step.narration` at step end (the
    reconciliation rule; the web folds the two).

    The tap never constructs a frame it might have to drop: it coalesces to one ≤1200-char slice
    per 0.12 s and leaves the rest buffered, so `Wire.say` can skip the drop gate honestly. `ai/`
    stays seq-blind - frames leave here with `after_seq=0` and `operations.live.emitter` stamps
    the real anchor.
    """

    def __init__(
        self,
        deps: TaskDeps,
        agent_name: str,
        *,
        flush_s: float = _MESSAGE_FLUSH_S,
        draft_clock: Callable[[], float] = time.monotonic,
        pour_output: bool = False,
        text_as_draft: bool = False,
        quiet_stream: bool = False,
    ) -> None:
        self._deps = deps
        self._agent = agent_name
        # A CONVERSATION reply: no task, a conversation - the frames are chat.*,
        # keyed by the conversation, and only the message lane and tool activity speak (no
        # drafts, no thinking pour, no model rounds: a reply is a message, not a step).
        self._chat = deps.task_id is None and deps.conversation_id is not None
        self._flush_s = flush_s  # injectable so tests can drive the tap without waiting on a clock
        self._draft_clock = draft_clock  # the scanners' parse throttle, injectable the same way
        self._pour_output = pour_output  # typed-deliverable agents stream their OUTPUT as the draft
        self._quiet = quiet_stream  # fan-out seats: prose to the tree's thinking pour, never the thread
        # Text-deliverable agents (the researcher): the model's PROSE is the document, so TextParts
        # pour to the draft channel and the message lane stays silent - the document composes in
        # the artifact pane, never as a chat bubble that then becomes a card.
        self._text_as_draft = text_as_draft
        self._buffer = ""
        self._started = False
        self._last_flush = 0.0
        # The streaming answer lives inside a file write's or a submitted document's args: one scanner per
        # tool-call part index, harvesting `content` as the provider composes the JSON.
        self._drafts: dict[int, _DraftStream] = {}
        # The micro-activity narrator (the progress tree):
        # in-flight tool calls (id → (journal name, label, monotonic start)), the model
        # round counter (rounds open lazily, on model-stream events only - see __call__),
        # and the thinking-pour buffer.
        self._calls: dict[str, tuple[str, str, float]] = {}
        # Which answer the run is on: a final-answer redirect starts the next one, as a
        # new message and a draft at a new path (`restart`).
        self._revision = 1
        self._round = 0
        self._round_open_at = 0.0
        self._round_settled = True
        self._thinking = ""
        self._thinking_flush = 0.0

    def _frame_kwargs(self, channel: str | None = None) -> dict[str, Any]:
        return {
            "task_id": self._deps.task_id,
            "step_id": self._deps.step_id,
            "project_id": self._deps.project_id,
            "at": dt.datetime.now(dt.UTC),
            "after_seq": 0,  # stamped by operations.live.emitter - ai/ does not know the record
            "channel": channel or f"{self._deps.step_id}:message",
        }

    def _chat_kwargs(self) -> dict[str, Any]:
        return {
            "conversation_id": self._deps.conversation_id,
            "at": dt.datetime.now(dt.UTC),
            "after_seq": 0,  # stamped by operations.live.emitter, exactly as a task frame's
        }

    async def __call__(
        self, ctx: RunContext[TaskDeps] | None, stream: AsyncIterable[AgentStreamEvent]
    ) -> None:
        # pydantic-ai invokes the handler once per graph NODE: the model-request node AND
        # the tool-execution node (`is_model_request_node(n) or is_call_tools_node(n)`).
        # So the round opens LAZILY, on the invocation's first model-stream event - the
        # earliest honest "Thinking…" there is. A tool-execution invocation carries only
        # FunctionTool*Events and opens nothing: rounds count model requests, and no
        # phantom round ever dangles unsettled over a run that already answered.
        round_opened = False
        async for event in stream:
            if not round_opened and isinstance(event, PartStartEvent | PartDeltaEvent | FinalResultEvent):
                round_opened = True
                await self._round_started()
            if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                await self._round_settled_now()
                await self._ingest(event.part.content, new_part=True)
            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                await self._ingest(event.delta.content_delta)
            elif isinstance(event, PartStartEvent) and isinstance(event.part, ThinkingPart):
                await self._pour_thinking(event.part.content or "")
            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, ThinkingPartDelta):
                await self._pour_thinking(event.delta.content_delta or "")
            elif isinstance(event, PartStartEvent) and isinstance(event.part, ToolCallPart):
                await self._round_settled_now()
                await self._call_started(event.index, event.part)
            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, ToolCallPartDelta):
                await self._call_grew(event.index, event.delta)
            elif isinstance(event, FunctionToolCallEvent):
                await self._tool_started(event)
            elif isinstance(event, FunctionToolResultEvent):
                await self._tool_ended(event)
        for draft in self._drafts.values():  # a response's calls are whole once its stream ends
            if not draft.raw:
                await self._pour_draft(draft, draft.scanner.finish())

    async def _call_started(self, index: int, part: ToolCallPart) -> None:
        """A tool call began streaming: a file write, a typed collection or a submitted document opens a draft."""
        if part.tool_name == "write_file":
            draft = _DraftStream(clock=self._draft_clock)
            self._drafts[index] = draft
            if isinstance(part.args, str) and part.args:
                await self._pour_draft(draft, draft.scanner.feed(part.args))
        elif self._pour_output and part.tool_name == "final_result":
            # A typed COLLECTION output: the deliverable IS the output tool's
            # streaming JSON args - poured verbatim to the draft channel.
            draft = _DraftStream(raw=True, path=self._draft_path("draft.collection.json"))
            self._drafts[index] = draft
            if isinstance(part.args, str) and part.args:
                await self._pour_draft(draft, part.args)
        elif self._pour_output and part.tool_name == SUBMIT_DOCUMENT:
            # A submitted document: its `content` argument, decoded as it arrives, is the draft.
            draft = _DraftStream(path=self._draft_path("draft.md"), clock=self._draft_clock)
            self._drafts[index] = draft
            if isinstance(part.args, str) and part.args:
                await self._pour_draft(draft, draft.scanner.feed(part.args))

    async def _call_grew(self, index: int, delta: ToolCallPartDelta) -> None:
        """More of a streaming call's arguments: an open draft takes them."""
        draft = self._drafts.get(index)
        if draft is not None and isinstance(delta.args_delta, str):
            await self._pour_draft(
                draft, delta.args_delta if draft.raw else draft.scanner.feed(delta.args_delta)
            )

    # ── the micro-activity narrator (the progress tree) ──────────────────────────────────

    async def _activity(self, **fields: Any) -> None:
        emit = self._deps.emit_event
        if emit is None:
            return
        if self._chat:
            if fields.get("kind") != "tool":
                return  # a reply narrates its tools only - rounds and reasoning are a step's tree
            await emit(
                ChatActivity(
                    phase=fields["phase"],
                    tool=str(fields.get("tool", "")),
                    tool_call_id=str(fields.get("tool_call_id", "")),
                    label=str(fields.get("label", "")),
                    ok=fields.get("ok"),
                    duration_ms=int(fields.get("duration_ms", 0)),
                    **self._chat_kwargs(),
                )
            )
            return
        await emit(Activity(**fields, **self._frame_kwargs(channel=f"{self._deps.step_id}:activity")))

    async def _round_started(self) -> None:
        self._round += 1
        self._round_open_at = time.monotonic()
        self._round_settled = False
        await self._activity(kind="model", phase="start", label=f"round {self._round}")

    async def _round_settled_now(self) -> None:
        """The round's first VISIBLE part arrived - "Thinking…" settles to "Thought for Ns".
        The buffered reasoning tail drains FIRST: the thought belongs before the visible
        part it produced, not after the run's close."""
        if self._round_settled:
            return
        self._round_settled = True
        while self._thinking:
            self._thinking_flush = 0.0
            await self._pour_thinking("")
        await self._activity(
            kind="model",
            phase="end",
            label=f"round {self._round}",
            duration_ms=int((time.monotonic() - self._round_open_at) * 1000),
        )

    async def _pour_thinking(self, text: str) -> None:
        """The model's reasoning, streaming - coalesced to the message-lane cadence. Emits
        nothing when models emit no thinking parts (the settings flip lights this up).
        Called with "" by close()'s drain loop - buffer-empty is the loop's exit."""
        self._thinking += text
        if not self._thinking:
            return
        if time.monotonic() - self._thinking_flush < self._flush_s:
            return
        chunk, self._thinking = (
            self._thinking[:MESSAGE_SLICE_CHARS],
            self._thinking[MESSAGE_SLICE_CHARS:],
        )
        self._thinking_flush = time.monotonic()
        await self._activity(kind="thinking", phase="delta", text=chunk)

    async def _tool_started(self, event: FunctionToolCallEvent) -> None:
        part = event.part
        tool = catalog.journal_name(part.tool_name)
        label = labels.label_for(part.tool_name, part.args_as_dict(), self._deps.frame)
        self._calls[part.tool_call_id] = (tool, label, time.monotonic())
        await self._activity(
            kind="tool", phase="start", tool=tool, label=label, tool_call_id=part.tool_call_id
        )

    async def _tool_ended(self, event: FunctionToolResultEvent) -> None:
        call_id = event.tool_call_id
        tool, label, started = self._calls.pop(call_id, ("", "", 0.0))
        if not tool:  # a result whose start we never saw (recovered stream) - name it honestly
            tool = catalog.journal_name(getattr(event.part, "tool_name", "") or "")
        await self._activity(
            kind="tool",
            phase="end",
            tool=tool,
            label=label,
            tool_call_id=call_id,
            ok=event.part.outcome == "success",  # retried, failed, denied or interrupted is not ok
            duration_ms=int((time.monotonic() - started) * 1000) if started else 0,
        )

    async def _ingest(self, text: str, *, new_part: bool = False) -> None:
        if not text:
            return
        if self._quiet:
            # A quiet seat's prose is working noise: it lights the tree (live-only), and the
            # thread's answer lane stays the system receipt's alone. Same paragraph law as the
            # other two lanes: a new text part joins the pour as a new paragraph (`_thinking_flush`
            # is non-zero once anything emitted, the lane's own `started` flag).
            if new_part and (self._thinking.strip() or self._thinking_flush):
                text = f"\n\n{text}"
            await self._pour_thinking(text)
            return
        if self._text_as_draft:
            # The prose IS the document (text lane): pour it to the draft channel.
            # Index -1 - TextParts across every round are ONE growing document, never keyed
            # by part index the way tool-call drafts are.
            draft = self._drafts.get(-1)
            if draft is None:
                draft = self._drafts[-1] = _DraftStream(raw=True, path=self._draft_path("draft.md"))
            if new_part and (draft.sent or draft.buffer.strip()):
                text = f"\n\n{text}"  # a new part after prose is a new paragraph, same document
            await self._pour_draft(draft, text)
            return
        if new_part and (self._buffer.strip() or self._started):
            self._buffer += "\n\n"  # a new text part after prose is a new paragraph, same message
        self._buffer += text
        if time.monotonic() - self._last_flush >= self._flush_s:
            await self._flush()

    async def _pour_draft(self, draft: _DraftStream, text: str) -> None:
        """Buffer newly-decoded document characters; emit on the same cadence as prose.
        Offsets make the frames self-describing - a dropped one leaves a visible seam the
        next durable snapshot heals, never a silent corruption."""
        emit = self._deps.emit_event
        if emit is None or self._chat:
            return  # a reply has no draft channel: its text is the message itself
        draft.buffer += text
        if not draft.buffer or time.monotonic() - draft.last_flush < self._flush_s:
            return
        chunk, draft.buffer = draft.buffer[:MESSAGE_SLICE_CHARS], draft.buffer[MESSAGE_SLICE_CHARS:]
        offset, draft.sent = draft.sent, draft.sent + len(chunk)
        draft.last_flush = time.monotonic()
        await emit(
            ArtifactDelta(
                path=draft.path or draft.scanner.path or "/work/draft.md",
                offset=offset,
                text=chunk,
                **self._frame_kwargs(channel=f"{self._deps.step_id}:draft"),
            )
        )

    async def _flush(self) -> None:
        emit = self._deps.emit_event
        if emit is None or not self._buffer:
            return
        if not self._started:
            self._started = True
            if self._chat:
                await emit(ChatStart(bot=self._agent, **self._chat_kwargs()))
            else:
                await emit(MessageStart(agent=self._agent, **self._frame_kwargs()))
        slice_, self._buffer = (
            self._buffer[:MESSAGE_SLICE_CHARS],
            self._buffer[MESSAGE_SLICE_CHARS:],
        )
        self._last_flush = time.monotonic()
        if self._chat:
            await emit(ChatDelta(text=slice_, **self._chat_kwargs()))
        else:
            await emit(MessageDelta(text=slice_, **self._frame_kwargs()))

    def _draft_path(self, name: str) -> str:
        """Where the publishing lane's draft streams: the first answer at `/work/<name>`, each
        redirected one under its own revision, so the web starts it afresh (it keys a draft on
        its path) and the card still reads `<name>`."""
        return f"/work/{name}" if self._revision == 1 else f"/work/revision-{self._revision}/{name}"

    async def _drain(self) -> None:
        """Emit everything buffered: the message, the thinking tail, every draft's tail."""
        while self._buffer:
            await self._flush()
        while self._thinking:
            self._thinking_flush = 0.0
            await self._pour_thinking("")
        for draft in self._drafts.values():
            while draft.buffer:  # drain the document tail - what streamed was written
                draft.last_flush = 0.0
                await self._pour_draft(draft, "")

    async def _end_message(self) -> None:
        emit = self._deps.emit_event
        if emit is None or not self._started:
            return
        if self._chat:
            await emit(ChatEnd(**self._chat_kwargs()))
        else:
            await emit(MessageEnd(**self._frame_kwargs()))

    async def append_text(self, text: str) -> None:
        """A paragraph the system adds after the model's text, streamed on the lane that text took."""
        await self._ingest(text, new_part=True)

    async def restart(self) -> None:
        """A final answer was redirected: the run takes a steer before it finishes, and
        what it said so far is no longer its answer. A correction is a new message, never a
        rewrite of what streamed: the buffered text drains, the message ends (its
        terminal), the drafts stop where they are, and the revised answer streams from scratch -
        a new message, a draft at a new path. The draft lane then holds only the newest (the web
        keys a draft on its path); on the message lane the superseded answer stays a settled
        bubble of its own until a reload, and the revised one is never glued to it."""
        if self._deps.emit_event is None:  # also breaks the drain loop: _flush can't consume
            return
        await self._drain()
        await self._end_message()
        self._started = False
        self._drafts.clear()
        self._revision += 1

    async def close(self) -> None:
        """After the run: drain what is buffered, then terminate the message - even on an
        exception, because whatever already streamed was said, and a said thing gets an end.
        Dangling tool STARTS are deliberately not synthesized shut (a parked ask_user's
        durable `gate.opened` is its honest terminal); the thinking tail drains."""
        if self._deps.emit_event is None:  # also breaks the drain loop: _flush can't consume
            return
        await self._drain()
        await self._end_message()
