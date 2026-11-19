# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The conversation stream's producer half (ADR-059): the message tap in CHAT mode.

Driven exactly like `test_message_tap.py` - pydantic-ai's real event objects, a stub emitter,
no model, no database, no wire - with deps that carry a conversation and no task. The
properties the Chat tab depends on: chat.start once, ordered append-only chat.delta frames
whose concatenation is exactly the text, chat.end once, tool activity as chat.activity, and
NOTHING of a step's tree (no model rounds, no thinking pour, no drafts) - a reply is a message.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from pydantic_ai.messages import (
    FinalResultEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
)

from prismoid.ai.agents.tap import MessageTap
from prismoid.ai.deps import TaskDeps
from prismoid.schemas.conversation import ChatLiveEvent
from prismoid.schemas.events import LiveEvent
from prismoid.schemas.ids import ConversationId


def _chat_deps(sink: list[LiveEvent | ChatLiveEvent]) -> TaskDeps:
    async def emit(event: LiveEvent | ChatLiveEvent) -> None:
        sink.append(event)

    return TaskDeps(conversation_id=ConversationId(uuid.uuid4()), emit_event=emit)


async def _drive(tap: MessageTap, events: list[Any]) -> None:
    async def stream() -> AsyncIterator[Any]:
        for event in events:
            yield event

    await tap(None, stream())
    await tap.close()


async def test_a_reply_streams_as_chat_frames() -> None:
    sink: list[LiveEvent | ChatLiveEvent] = []
    tap = MessageTap(_chat_deps(sink), "release-notes-scribe", flush_s=0.0)
    await _drive(
        tap,
        [
            FinalResultEvent(tool_name=None, tool_call_id=None),
            PartStartEvent(index=0, part=TextPart(content="Karl Schwarzschild ")),
            PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="solved the field equations.")),
        ],
    )
    types = [e.type for e in sink]
    assert types[0] == "chat.start" and types[-1] == "chat.end"
    assert set(types[1:-1]) == {"chat.delta"}
    assert "".join(getattr(e, "text", "") for e in sink if e.type == "chat.delta") == (
        "Karl Schwarzschild solved the field equations."
    )
    start = sink[0]
    assert getattr(start, "bot", "") == "release-notes-scribe"
    assert start.lane == "live" and getattr(start, "after_seq", None) == 0  # operations stamps the anchor
    assert all(e.type.startswith("chat.") for e in sink)  # nothing of a step's tree


async def test_tools_narrate_as_chat_activity_and_nothing_else_leaks() -> None:
    sink: list[LiveEvent | ChatLiveEvent] = []
    tap = MessageTap(_chat_deps(sink), "scribe", flush_s=0.0)
    call = ToolCallPart(tool_name="web_search", args={"query": "schwarzschild"}, tool_call_id="c1")
    await _drive(
        tap,
        [
            FinalResultEvent(tool_name=None, tool_call_id=None),
            PartStartEvent(index=0, part=ThinkingPart(content="let me look")),
            FunctionToolCallEvent(part=call),
            FunctionToolResultEvent(
                part=ToolReturnPart(tool_name="web_search", content="…", tool_call_id="c1")
            ),
            PartStartEvent(index=1, part=TextPart(content="Found it.")),
        ],
    )
    acts = [e for e in sink if e.type == "chat.activity"]
    assert [getattr(a, "phase", "") for a in acts] == ["start", "end"]
    assert getattr(acts[0], "tool", "") == "web.search"
    assert getattr(acts[1], "ok", None) is True
    # no model rounds, no thinking pour: those are a step's progress tree, never a chat's
    assert not any(e.type == "activity" for e in sink)
    assert [e.type for e in sink if e.type.startswith("chat.") and e.type != "chat.activity"] == [
        "chat.start",
        "chat.delta",
        "chat.end",
    ]


async def test_a_task_step_still_speaks_the_message_lane() -> None:
    """The mode is decided by the deps: a task id means a step, and a step's frames are the
    task lane's - the chat arm changes nothing the task page already renders."""
    from prismoid.schemas.ids import StepId, TaskId

    sink: list[LiveEvent | ChatLiveEvent] = []

    async def emit(event: LiveEvent | ChatLiveEvent) -> None:
        sink.append(event)

    deps = TaskDeps(task_id=TaskId(uuid.uuid4()), step_id=StepId(uuid.uuid4()), emit_event=emit)
    tap = MessageTap(deps, "writer", flush_s=0.0)
    await _drive(tap, [PartStartEvent(index=0, part=TextPart(content="hello"))])
    assert [e.type for e in sink if e.type.startswith("message.")] == [
        "message.start",
        "message.delta",
        "message.end",
    ]
    assert not any(e.type.startswith("chat.") for e in sink)
