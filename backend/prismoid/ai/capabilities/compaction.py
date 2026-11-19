# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Within a step, the context stays under its model's window: before each model request the history
is brought back under its threshold in three moves, cheapest first.

1. **Spill.** A tool result bigger than any tool's own slice (`SPILL_AT`) goes whole to a file under
   `/work` and the model reads its head and tail and the file's name. Only a result the model has
   not seen yet is ever that big, so a spill never rewrites a cached prefix, on any provider. With no
   frame the result is cut to its head and tail.
2. **Clear.** Once the history passes `TRIGGER_SHARE` of the usable window, every tool result but
   the newest `KEEP_RESULTS` shrinks to its first line, in one batch (each clear re-writes the cached
   prefix once, so it comes rarely and large). A browser seat also collapses old pages in batches
   (`collapse_old_pages`). A request to a row whose server clears old results itself for the class
   (`providers.server_clears`) is never edited here; the choice is made per request by the row the
   chain tries first, so a run that failed over from such a row is bounded here again.
3. **Hand off.** If clearing was not enough, the oldest exchanges are replaced by a handoff kept in
   the first request, and the run goes on from the newest exchanges. A cheap model writes it (the
   `summarizer`, a logged capability operation, so a replay reads the recorded summary and buys
   nothing); when that call fails, code writes it from each call's first line, the lines the run was
   told and its own notes.

The size is the provider's own count at the last response plus an estimate of what came after it
(`history_tokens`); a history with no reported count is estimated whole, and a replayed response
carries its recorded usage, so a replay measures the same. A handoff is due when the measured size,
less the clear's estimated saving, still passes the trigger; it keeps at least the last exchange.
The threshold comes from the serving rows' `context_window` in the catalog, less
the output ceiling; a window the catalog does not know gets the spill alone. Every move is a function
of the history and the logged summary (the spill's file name is the call's own id), so a replayed run
sends the same requests and its fingerprints hold.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import KW_ONLY, dataclass
from typing import TYPE_CHECKING, Final, cast

import logfire
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, durable_operation
from pydantic_ai.messages import (
    BinaryContent,
    FileUrl,
    ModelMessage,
    ModelRequest,
    ModelRequestPart,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_core import to_json

from prismoid.ai.deps import FrameLike, TaskDeps
from prismoid.ai.frames.paper import PaperFull
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.providers import DEFAULT_MAX_TOKENS, server_clears
from prismoid.ai.gateway.resolve import head_row
from prismoid.ai.tools.browser import FENCE_CLOSE, TOOL_NAMES
from prismoid.ai.tools.call_rows import ran
from prismoid.ai.tools.frame_tools import writable_target
from prismoid.ai.tools.output import capped, spill_path
from prismoid.errors import PolicyDenied
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.tokens import TOKENS

if TYPE_CHECKING:
    from pydantic_ai.models import Model, ModelRequestContext

# The summarizer's run over a transcript, answering the handoff text (`agents/mounts.py` wires it).
type Summarize = Callable[[str], Awaitable[str]]

# A result above this many tokens is spilled: above any tool's own slice (`web_fetch` hands back
# 24k characters, about 6k tokens), so it catches only output no tool bounded.
SPILL_AT: Final = 16_000
# The share of the usable window at which old tool results are cleared.
TRIGGER_SHARE: Final = 0.5
# Tool results a clear keeps whole, newest first: as many as the server-side clear keeps.
KEEP_RESULTS: Final = 6
# What one image or file costs the model to read, in tokens.
FILE_TOKENS: Final = 1_600
# The handoff's lines (the newest kept) and how long each may be.
HANDOFF_LINES: Final = 40
LINE_CHARS: Final = 200
# What the summarizer reads at most, head and tail: about 50k tokens of the exchanges it replaces.
TRANSCRIPT_CHARS: Final = 200_000
PART_CHARS: Final = 4_000
# A result at or under this many tokens is never cleared: a clear would save nothing, and a cleared
# result (one short line) is never cleared again.
STUB_TOKENS: Final = 100

CLEARED: Final = "[cleared to keep this run within its context window; it began: "
HANDOFF: Final = "[Handoff: the start of this run was summarized to keep it within its context window.]"
# The handoff's mark, on its prompt's metadata (the model never reads it): the lines code wrote.
HANDOFF_KEY: Final = "prismoid.compaction.handoff"

_COLLAPSED_SUFFIX: Final = " (collapsed)"
# A browser return carries a page when it carries the fence's closing line: a snapshot does, and
# so does the call that ended a response (it brings the page back). Nothing on the page can forge
# one - page text is `fence_guarded`, and a title or an error is a single line.
_PAGE_END: Final = f"\n{FENCE_CLOSE}"
# Full pages allowed before a collapse fires. Every collapse re-writes the cached history after
# the first page it touches, so it fires rarely and takes a whole batch at once.
COLLAPSE_HIGH_WATER: Final = 8


@dataclass
class Compaction(AbstractCapability[TaskDeps]):
    """Keeps the run's history under its threshold: spill, then clear, then hand off."""

    window: int  # usable input tokens (the window less the output ceiling); 0 when unknown
    summarize: Summarize | None = None  # the summarizer's run; None: code writes every handoff
    _: KW_ONLY
    cleared_rows: frozenset[str] = frozenset()  # refs whose server clears old results for this class
    id: str | None = "compaction"  # pinned: the call log names its operations by it

    @classmethod
    def for_seat(
        cls,
        model_class: ModelClass,
        catalog: Sequence[ModelSpec],
        served: ModelSpec | None,
        summarize: Summarize | None = None,
    ) -> Compaction:
        """The bound for the row serving the run, or for every row a mixed chain may fall to."""
        seats = [served] if served is not None else [m for m in catalog if m.serves(model_class)]
        cleared = frozenset(seat.ref for seat in seats if server_clears(seat.protocol, model_class))
        windows = [seat.context_window for seat in seats if seat.context_window > 0]
        window = max(min(windows) - DEFAULT_MAX_TOKENS, 0) if windows else 0
        return cls(window=window, summarize=summarize, cleared_rows=cleared)

    def edits(self, model: Model) -> bool:
        """Whether this request may be edited here: the row it goes to first does not clear on its server."""
        row = head_row(model)
        return row is None or row.ref not in self.cleared_rows

    @classmethod
    def get_serialization_name(cls) -> str | None:
        return None  # holds a callable; never spec-constructed

    async def before_model_request(
        self, ctx: RunContext[TaskDeps], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        """The history this request sends, written back as the run's history."""
        messages = await spilled(list(request_context.messages), ctx.deps.frame)
        if self.edits(request_context.model):
            if ctx.deps.browser is not None:
                messages = collapse_old_pages(keep=ctx.deps.browser.snapshot_window)(messages)
            if self.window:
                messages = await self._compacted(ctx, messages, int(self.window * TRIGGER_SHARE))
        request_context.messages = messages.copy()
        ctx.messages[:] = messages
        return request_context

    async def _compacted(
        self, ctx: RunContext[TaskDeps], messages: list[ModelMessage], trigger: int
    ) -> list[ModelMessage]:
        """`compacted`, its handoff written by the summarizer when there is one."""
        cleared = compacted(messages, trigger)
        if (cut := handoff_cut(messages, cleared, trigger)) is None:
            return cleared
        summary = None if self.summarize is None else await self._summary(ctx, transcript(cleared, cut))
        return handed_off(cleared, cut, summary)

    @durable_operation(name="handoff")
    async def _summary(self, ctx: RunContext[TaskDeps], text: str) -> str | None:
        """The summarizer's handoff of `text`, logged; a failed call is logged as one (None)."""
        assert self.summarize is not None
        try:
            return (await self.summarize(text)).strip() or None
        except Exception:  # fail-soft: the code handoff stands in
            logfire.exception("the summarizer could not write a handoff; code wrote it")
            return None


def history_tokens(messages: Sequence[ModelMessage]) -> int:
    """What the history costs the model to read: the provider's own count at the last response that
    reported one (its input and output), plus an estimate of what came after it."""
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        if isinstance(message, ModelResponse) and message.usage.input_tokens > 0:
            return message.usage.input_tokens + message.usage.output_tokens + estimated(messages[i + 1 :])
    return estimated(messages)


def estimated(messages: Sequence[ModelMessage]) -> int:
    """The estimated tokens of `messages` (the shared estimate, `TOKENS`)."""
    return sum(_part_tokens(part) for message in messages for part in message.parts)


def _part_tokens(part: object) -> int:
    if isinstance(part, ToolCallPart):
        return TOKENS.count(part.tool_name) + TOKENS.count(part.args_as_json_str())
    return _content_tokens(getattr(part, "content", ""))


def _content_tokens(content: object) -> int:
    if isinstance(content, str):
        return TOKENS.count(content)
    if isinstance(content, BinaryContent | FileUrl):
        return FILE_TOKENS
    if isinstance(content, list | tuple):
        return sum(_content_tokens(item) for item in cast("Sequence[object]", content))
    return TOKENS.count(to_json(content, fallback=str).decode())


async def spilled(messages: list[ModelMessage], frame: FrameLike | None) -> list[ModelMessage]:
    """Every tool result above `SPILL_AT` spilled whole to its call's file, its head and tail kept."""
    out: list[ModelMessage] = []
    for message in messages:
        if isinstance(message, ModelRequest) and any(_spills(part) for part in message.parts):
            message = dataclasses.replace(
                message, parts=[await _spilled_part(part, frame) for part in message.parts]
            )
        out.append(message)
    return out


async def _spilled_part(part: ModelRequestPart, frame: FrameLike | None) -> ModelRequestPart:
    if not _spills(part):
        return part
    assert isinstance(part, ToolReturnPart) and isinstance(part.content, str)
    return dataclasses.replace(part, content=await _spill(part, part.content, frame))


def _spills(part: ModelRequestPart) -> bool:
    return (
        isinstance(part, ToolReturnPart)
        and isinstance(part.content, str)
        and TOKENS.count(part.content) > SPILL_AT
    )


async def _spill(part: ToolReturnPart, text: str, frame: FrameLike | None) -> str:
    """`text`'s head and tail, naming the file that holds all of it; fail-soft: a spill that cannot be
    written costs only the file's name, never the result."""
    if frame is None:
        return capped(text)
    try:
        target = await writable_target(frame, spill_path(part.tool_name, part.tool_call_id))
        if target is None:
            return capped(text)
        await frame.write_file(target, text)
    except (OSError, ValueError, PolicyDenied, PaperFull):
        return capped(text)
    # The file leads, so a later clear, which keeps a result's first line, keeps where it is.
    head = f"[The whole result is in {frame.paths.render(target)}; its head and tail follow.]"
    return f"{head}\n{capped(text, read_on='Read the part you need from the file named above.')}"


def collapse_old_pages(keep: int) -> Callable[[list[ModelMessage]], list[ModelMessage]]:
    """Past `COLLAPSE_HIGH_WATER` page returns, all but the newest `keep` shrink to their first line plus
    `(collapsed)`, in one batch (cache-friendly); idempotent, nothing dropped (seats without server clearing)."""

    def _collapse(messages: list[ModelMessage]) -> list[ModelMessage]:
        full = [
            (mi, pi)
            for mi, message in enumerate(messages)
            for pi, part in enumerate(message.parts)
            if ran(part)  # a refused call sent back to the model is not a page
            and part.tool_name in TOOL_NAMES
            and isinstance(part.content, str)
            and _PAGE_END in part.content
        ]
        if len(full) <= COLLAPSE_HIGH_WATER:
            return messages
        out = list(messages)
        for mi, pi in full[:-keep] if keep > 0 else full:
            parts = list(out[mi].parts)
            part = parts[pi]
            assert isinstance(part, ToolReturnPart) and isinstance(part.content, str)
            parts[pi] = dataclasses.replace(
                part, content=f"{part.content.splitlines()[0]}{_COLLAPSED_SUFFIX}"
            )
            out[mi] = dataclasses.replace(out[mi], parts=parts)
        return out

    return _collapse


def compacted(messages: list[ModelMessage], trigger: int) -> list[ModelMessage]:
    """`messages` with old tool results cleared in one batch once they pass `trigger`."""
    return messages if history_tokens(messages) <= trigger else _cleared(messages)


def handoff_cut(messages: list[ModelMessage], cleared: list[ModelMessage], trigger: int) -> int | None:
    """Where the kept exchanges start when clearing left `messages` over `trigger`: the oldest response
    whose tail fits half the trigger; None when no handoff is due or nothing before it can go."""
    if history_tokens(messages) - (estimated(messages) - estimated(cleared)) <= trigger:
        return None
    responses = [i for i, message in enumerate(cleared) if i > 0 and isinstance(message, ModelResponse)]
    if not isinstance(cleared[0], ModelRequest) or not responses:
        return None
    start = responses[-1]
    for i in reversed(responses[:-1]):
        if estimated(cleared[i:]) > trigger // 2:
            break
        start = i
    return start if start > 1 else None


def _cleared(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Every tool result but the newest `KEEP_RESULTS` shrunk to its first line and its file's name."""
    found = [
        (mi, pi)
        for mi, message in enumerate(messages)
        for pi, part in enumerate(message.parts)
        if isinstance(part, ToolReturnPart)
    ]
    stale = set(found[:-KEEP_RESULTS])
    out: list[ModelMessage] = []
    for mi, message in enumerate(messages):
        if isinstance(message, ModelRequest) and any((mi, pi) in stale for pi in range(len(message.parts))):
            parts = [_stub(part) if (mi, pi) in stale else part for pi, part in enumerate(message.parts)]
            message = dataclasses.replace(message, parts=parts)
        out.append(message)
    return out


def _stub(part: ModelRequestPart) -> ModelRequestPart:
    """A cleared result: its first line (a spilled one's names its file)."""
    if not isinstance(part, ToolReturnPart) or _content_tokens(part.content) <= STUB_TOKENS:
        return part
    return dataclasses.replace(part, content=f"{CLEARED}{_line(part.model_response_str())}]")


def handed_off(messages: list[ModelMessage], cut: int, summary: str | None) -> list[ModelMessage]:
    """The first request carrying the handoff of `messages[1:cut]` (the summary, else one code wrote),
    then the exchanges from `cut` on."""
    head = messages[0]
    assert isinstance(head, ModelRequest)
    earlier = next((mark for part in head.parts if (mark := _handoff(part)) is not None), Written())
    kept = [part for part in head.parts if _handoff(part) is None]
    written = Written() if summary else _written(earlier, messages[1:cut])
    said = f"{HANDOFF}\n{summary or written.text()}"
    handoff = SystemPromptPart(said, metadata={HANDOFF_KEY: dataclasses.asdict(written)})
    return [dataclasses.replace(head, parts=[*kept, handoff]), *messages[cut:]]


def transcript(messages: list[ModelMessage], cut: int) -> str:
    """What the summarizer reads: the ask, the earlier handoff, then every part `messages[1:cut]`
    holds as one line each, every one cut to its head and tail."""
    lines = [
        f"[{_speaker(part)}] {capped(_said(part), cap=PART_CHARS)}"
        for m in messages[:cut]
        for part in m.parts
    ]
    return capped("\n".join(lines), cap=TRANSCRIPT_CHARS)


def _speaker(part: object) -> str:
    if isinstance(part, ToolCallPart):
        return f"call {part.tool_name}"
    if isinstance(part, ToolReturnPart):
        return f"result {part.tool_name}"
    if isinstance(part, TextPart):
        return "the agent"
    if isinstance(part, SystemPromptPart):
        return "the earlier handoff"
    return "the person" if isinstance(part, UserPromptPart) else "note"


def _said(part: object) -> str:
    if isinstance(part, ToolCallPart):
        return part.args_as_json_str()
    if isinstance(part, ToolReturnPart):
        return part.model_response_str()
    content = getattr(part, "content", "")
    return content if isinstance(content, str) else to_json(content, fallback=str).decode()


@dataclass(frozen=True, slots=True)
class Written:
    """A handoff code wrote: its lines, the newest `HANDOFF_LINES`, and how many older ones it dropped."""

    lines: list[str] = dataclasses.field(default_factory=list[str])
    skipped: int = 0

    def text(self) -> str:
        """The handoff as the model reads it."""
        dropped = [f"({self.skipped} earlier lines not kept)"] if self.skipped else []
        return "\n".join(["What happened in it, oldest first:", *dropped, *self.lines])


def _handoff(part: object) -> Written | None:
    """The handoff `part` carries, by its mark; None when it is no handoff."""
    if not isinstance(part, SystemPromptPart) or part.metadata is None:
        return None
    mark = part.metadata.get(HANDOFF_KEY)
    return None if mark is None else Written(**mark)


def _written(earlier: Written, dropped: Sequence[ModelMessage]) -> Written:
    """An earlier handoff's lines, then one line per call, prompt and note dropped now, the newest
    `HANDOFF_LINES` kept."""
    lines = list(earlier.lines)
    results = {
        part.tool_call_id: _line(part.model_response_str())
        for message in dropped
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    }
    for message in dropped:
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                lines.append(f"- called {part.tool_name}: {results.get(part.tool_call_id, 'no result')}")
            elif isinstance(part, TextPart) and part.content.strip():
                lines.append(f"- you noted: {_line(part.content)}")
            elif isinstance(part, UserPromptPart) and isinstance(part.content, str):
                lines.append(f"- you were told: {_line(part.content)}")
    skipped = earlier.skipped + max(len(lines) - HANDOFF_LINES, 0)
    return Written(lines[-HANDOFF_LINES:], skipped)


def _line(text: str) -> str:
    """The first non-blank line of `text`, at most `LINE_CHARS` long."""
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first if len(first) <= LINE_CHARS else f"{first[: LINE_CHARS - 3]}..."
