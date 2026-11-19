# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The `tool.called` rows a tool call writes, the port they go out through, and what a run's
history says about its calls.

`ToolRecorder` (`ai/capabilities/tool_record.py`) opens a record per call (`recording`) and writes
its rows once. Code under the call speaks only when the row depends on how the call went (`say`):
which engine served, where a snapshot landed, a refusal it returned as text. A toolset driven by
hand, outside a run, journals what it says directly. The history predicates (`ran`, `never_ran`,
`declined`) read a call's return the way those rows were written, and the frame's egress refusals
become rows here too (`journal_egress_denials`).
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Final, Protocol, TypeGuard

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelRequestPart,
    ModelResponsePart,
    ToolReturn,
    ToolReturnPart,
)

from prismoid.ai.tools import labels
from prismoid.schemas.computer_use import BROWSER_BACKGROUND
from prismoid.schemas.guard import NOT_RUN_METADATA


class JournalTool(Protocol):
    """How a tool call reaches the record: an intent `operations` turns into a durable event (it
    assigns the `seq`), so `ai/` stays persistence-free."""

    async def __call__(
        self,
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        *,
        ref: str | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class Said:
    """One `tool.called` row as the code that ran the call words it. An empty summary or call id,
    or a zero duration, is the recorder's to fill from the call; `ref` names what the call kept."""

    tool: str
    ok: bool
    summary: str = ""
    duration_ms: int = 0
    denied_reason: str = ""
    tool_call_id: str = ""
    ref: str | None = None

    @classmethod
    def failed(cls, tool: str, exc: BaseException, summary: str = "") -> Said:
        """A not-ok row whose reason is the exception, worded as the record words every failure."""
        return cls(tool, False, summary, denied_reason=f"{type(exc).__name__}: {exc}")


_SAYING: ContextVar[list[Said] | None] = ContextVar("prismoid_tool_record", default=None)


@contextmanager
def recording() -> Generator[list[Said]]:
    """Collect the rows the code under one tool call says about it, for the recorder to write."""
    said: list[Said] = []
    token = _SAYING.set(said)
    try:
        yield said
    finally:
        _SAYING.reset(token)


async def journal_row(journal: JournalTool | None, row: Said) -> None:
    """Write one row, its reason scrubbed: an exception's prose carries whatever URL it failed on."""
    if journal is None:
        return  # no task context (the selftest): nothing to journal to
    reason = labels.scrub_reason(row.denied_reason)
    await journal(row.tool, row.ok, row.summary, row.duration_ms, reason, row.tool_call_id, ref=row.ref)


async def say(journal: JournalTool | None, row: Said) -> None:
    """Hand a row to the call's open record, or journal it now when no record is open."""
    said = _SAYING.get()
    if said is None:
        await journal_row(journal, row)
    else:
        said.append(row)


def ran(part: ModelRequestPart | ModelResponsePart) -> TypeGuard[ToolReturnPart]:
    """A tool's return for a call that ran: a retried one was sent back to the model and never did."""
    return isinstance(part, ToolReturnPart) and part.outcome != "retried"


def not_run(text: str) -> ToolReturn:
    """The answer of a call that never ran: `text` to the model, marked so a recall skips it."""
    return ToolReturn(text, metadata=dict(NOT_RUN_METADATA))


def never_ran(part: ToolReturnPart) -> bool:
    """A return for a call that never ran: marked `not_run`, or an approval a person or the budget denied."""
    return part.outcome == "denied" or part.metadata == NOT_RUN_METADATA


# The record name of a frame egress refusal: the frame's own row, never a tool a model calls.
EGRESS: Final = "frame.egress"

# A person's Deny on an approval, as the run reads it: an answer to continue on, never asked again.
DECLINED: Final = (
    "the person declined this action - that is their answer, not a wall: do not ask for it again; "
    "finish without it (partial if the goal needed it), say what was skipped, and give them what you "
    "prepared for it (the text, the values) so they can use it themselves"
)


def declined_text(act: str | None = None) -> str:
    """A Deny's answer to the model: `DECLINED`, marked with a digest of the `act` declined when the
    tool's name alone does not say what was declined (a browser click: which element, on which page).
    A digest, never the act's words: a page's text stays inside the fences."""
    if act is None:
        return DECLINED
    return f"{DECLINED} [act {hashlib.sha256(act.encode()).hexdigest()[:12]}]"


def declined(messages: Iterable[ModelMessage], tool: str | None = None, act: str | None = None) -> bool:
    """Whether a person declined a call to `tool` (any tool when None) in this run's history; with
    `act`, only a decline of that same act (`declined_text`)."""
    return any(
        isinstance(part, ToolReturnPart)
        and part.outcome == "denied"
        and isinstance(part.content, str)
        and (part.content == DECLINED or part.content.startswith(f"{DECLINED} [act "))
        and tool in (None, part.tool_name)
        and (act is None or part.content == declined_text(act))
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
    )


async def journal_egress_denials(journal: JournalTool | None, denials: list[dict[str, str]]) -> None:
    """One row per host and reason the frame's egress refused, saying how often; none for Chromium's
    own background calls (`BROWSER_BACKGROUND`)."""
    seen = Counter(
        (denial.get("host", ""), denial.get("reason", ""))
        for denial in denials
        if denial.get("host", "").casefold() not in BROWSER_BACKGROUND
    )
    for (host, reason), times in seen.items():
        await journal_row(
            journal,
            Said(
                EGRESS,
                ok=False,
                summary=labels.egress_label(host),
                denied_reason=f"{reason} ({times} times)" if times > 1 else reason,
            ),
        )
