# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The message lane's tap - the visible-text stream becomes message.* frames.

The contract promised `message.start/delta/end` and the web reducer renders them;
the producer is `ai/agents/tap.MessageTap`. These tests drive the tap with pydantic-ai's real
event objects and a stub emitter - no model, no database, no wire - and pin the properties the
frontend depends on: one start, ordered append-only deltas whose concatenation is exactly the
text, one end, and silence for runs that never produce visible text (a judge's run must not put
frames in a user's thread).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

import pytest
from pydantic_ai.messages import (
    FinalResultEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)

from prismoid.ai.agents.tap import MESSAGE_SLICE_CHARS, MessageTap
from prismoid.ai.deps import TaskDeps
from prismoid.schemas.conversation import ChatLiveEvent
from prismoid.schemas.events import Activity, ArtifactDelta, LiveEvent
from prismoid.schemas.ids import StepId, TaskId


def _deps(sink: list[LiveEvent]) -> TaskDeps:
    async def emit(event: LiveEvent | ChatLiveEvent) -> None:
        # a task step's tap never speaks a chat frame (test_chat_stream pins the other mode)
        assert not event.type.startswith("chat.")
        sink.append(event)  # pyright: ignore[reportArgumentType]

    return TaskDeps(
        task_id=TaskId(uuid.uuid4()),
        step_id=StepId(uuid.uuid4()),
        emit_event=emit,
    )


async def _invoke(tap: MessageTap, events: list[Any]) -> None:
    """One handler invocation - pydantic-ai calls the tap once per graph node, so a run is
    a sequence of these (model request, tool execution, model request, …)."""

    async def stream() -> AsyncIterator[Any]:
        for event in events:
            yield event

    await tap(None, stream())


async def _drive(tap: MessageTap, events: list[Any]) -> None:
    await _invoke(tap, events)
    await tap.close()


def _acts(sink: list[LiveEvent], kind: str) -> list[Activity]:
    """The activity frames of one kind - the tree's rows, as the wire saw them."""
    return [e for e in sink if isinstance(e, Activity) and e.kind == kind]


async def test_text_stream_becomes_one_message() -> None:
    sink: list[LiveEvent] = []
    tap = MessageTap(_deps(sink), "writer", flush_s=0.0)

    await _drive(
        tap,
        [
            PartStartEvent(index=0, part=TextPart(content="Reading the brief")),
            PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=", then the numbers.")),
        ],
    )

    kinds = [e.type for e in sink if e.type.startswith("message.")]
    assert kinds[0] == "message.start"
    assert kinds[-1] == "message.end"
    assert set(kinds[1:-1]) == {"message.delta"}
    text = "".join(e.text for e in sink if e.type == "message.delta")
    assert text == "Reading the brief, then the numbers."
    start = next(e for e in sink if e.type == "message.start")
    assert getattr(start, "agent", "") == "writer"
    assert start.channel.endswith(":message")
    assert start.lane == "live"


async def test_a_silent_request_still_narrates_its_round() -> None:
    """The old law was "no visible text → no frames"; the tree inverted it:
    a model request that says nothing visible is exactly when the user most needs
    "Thinking…" - its FinalResultEvent opens one model-round start, on the activity
    channel, and NO message frames (an empty message is still not a message)."""
    sink: list[LiveEvent] = []
    tap = MessageTap(_deps(sink), "judge")
    await _drive(tap, [FinalResultEvent(tool_name=None, tool_call_id=None)])
    assert [e.type for e in sink if e.type.startswith("message.")] == []
    rounds = _acts(sink, "model")
    assert len(rounds) == 1
    start = rounds[0]
    assert getattr(start, "phase", "") == "start"
    assert getattr(start, "label", "") == "round 1"
    assert start.channel.endswith(":activity")


async def test_a_tool_execution_invocation_opens_no_round() -> None:
    """pydantic-ai invokes the handler once per graph NODE - the tool-execution node too.
    Only model-stream events open a round: request → tools → request counts exactly two
    rounds, and the tool node leaves no phantom "Thinking…" dangling over the answer."""
    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    sink: list[LiveEvent] = []
    tap = MessageTap(_deps(sink), "writer", flush_s=0.0)
    await _invoke(tap, [PartStartEvent(index=0, part=TextPart(content="Looking."))])
    await _invoke(
        tap,
        [
            FunctionToolCallEvent(
                part=ToolCallPart(
                    tool_name="read_file", args={"path": "/task/inputs/a.md"}, tool_call_id="tc1"
                )
            ),
            FunctionToolResultEvent(
                part=ToolReturnPart(tool_name="read_file", content="…", tool_call_id="tc1")
            ),
        ],
    )
    await _invoke(tap, [PartStartEvent(index=0, part=TextPart(content="The answer."))])
    await tap.close()

    model = _acts(sink, "model")
    assert [(getattr(e, "phase", ""), getattr(e, "label", "")) for e in model] == [
        ("start", "round 1"),
        ("end", "round 1"),
        ("start", "round 2"),
        ("end", "round 2"),
    ]
    assert len(_acts(sink, "tool")) == 2  # the tool node still narrates its calls


async def test_the_thinking_tail_lands_before_the_round_settles() -> None:
    """The reasoning belongs before the visible part it produced: when the round settles,
    the buffered thinking tail drains FIRST - never parked until the run's close."""
    from pydantic_ai.messages import ThinkingPart, ThinkingPartDelta

    sink: list[LiveEvent] = []
    tap = MessageTap(_deps(sink), "writer")  # real cadence - the second chunk buffers
    await _invoke(
        tap,
        [
            PartStartEvent(index=0, part=ThinkingPart(content="First thought. ")),
            PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta="Buffered tail.")),
            PartStartEvent(index=1, part=TextPart(content="The answer.")),
        ],
    )
    text = "".join(getattr(e, "text", "") for e in _acts(sink, "thinking"))
    assert text == "First thought. Buffered tail."
    marks = [(e.type, getattr(e, "kind", ""), getattr(e, "phase", "")) for e in sink]
    settle = marks.index(("activity", "model", "end"))
    last_thinking = max(i for i, mark in enumerate(marks) if mark[1] == "thinking")
    assert last_thinking < settle


async def test_the_round_settles_when_the_first_visible_part_arrives() -> None:
    sink: list[LiveEvent] = []
    tap = MessageTap(_deps(sink), "writer", flush_s=0.0)
    await _drive(tap, [PartStartEvent(index=0, part=TextPart(content="Here."))])
    model = _acts(sink, "model")
    assert [getattr(e, "phase", "") for e in model] == ["start", "end"]
    assert getattr(model[1], "duration_ms", -1) >= 0


async def test_thinking_parts_pour_on_the_activity_channel() -> None:
    """Plumbed now, enabled later (user-locked): synthetic ThinkingParts stream as
    kind="thinking" deltas; when models emit none, this lane is silent."""
    from pydantic_ai.messages import ThinkingPart, ThinkingPartDelta

    sink: list[LiveEvent] = []
    tap = MessageTap(_deps(sink), "writer", flush_s=0.0)
    await _drive(
        tap,
        [
            PartStartEvent(index=0, part=ThinkingPart(content="Consider the brief")),
            PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta=", then outline.")),
        ],
    )
    thinking = _acts(sink, "thinking")
    assert thinking, "thinking parts must reach the wire"
    text = "".join(getattr(e, "text", "") for e in thinking)
    assert text == "Consider the brief, then outline."
    # Thinking is not a visible part - the round stays open ("Thinking…") throughout.
    assert [getattr(e, "phase", "") for e in _acts(sink, "model")] == ["start"]


async def test_tool_calls_narrate_start_and_end_with_the_same_id() -> None:
    from pydantic_ai.messages import ToolCallPart as TCP
    from pydantic_ai.messages import ToolReturnPart

    sink: list[LiveEvent] = []
    tap = MessageTap(_deps(sink), "writer", flush_s=0.0)
    call = TCP(tool_name="read_file", args={"path": "/task/inputs/brief.md"}, tool_call_id="tc9")
    await _drive(
        tap,
        [
            FunctionToolCallEvent(part=call),
            FunctionToolResultEvent(
                part=ToolReturnPart(tool_name="read_file", content="…", tool_call_id="tc9")
            ),
        ],
    )
    tools = _acts(sink, "tool")
    assert [getattr(e, "phase", "") for e in tools] == ["start", "end"]
    assert all(getattr(e, "tool", "") == "frame.read_file" for e in tools)
    assert all(getattr(e, "label", "") == "/task/inputs/brief.md" for e in tools)
    assert all(getattr(e, "tool_call_id", "") == "tc9" for e in tools)
    assert getattr(tools[1], "ok", None) is True


@pytest.mark.parametrize("outcome", ["retried", "failed", "denied", "interrupted"])
async def test_a_call_that_did_not_succeed_is_an_end_with_ok_false(
    outcome: Literal["retried", "failed", "denied", "interrupted"],
) -> None:
    from pydantic_ai.messages import ToolCallPart as TCP
    from pydantic_ai.messages import ToolReturnPart

    sink: list[LiveEvent] = []
    tap = MessageTap(_deps(sink), "writer", flush_s=0.0)
    await _drive(
        tap,
        [
            FunctionToolCallEvent(
                part=TCP(tool_name="publish", args={"type": "markdown", "path": "/w.md"}, tool_call_id="tc1")
            ),
            FunctionToolResultEvent(
                part=ToolReturnPart(
                    tool_name="publish", content="unknown type", tool_call_id="tc1", outcome=outcome
                )
            ),
        ],
    )
    end = next(e for e in _acts(sink, "tool") if getattr(e, "phase", "") == "end")
    assert getattr(end, "ok", None) is False


async def test_a_hallucinated_tools_arg_values_never_reach_the_wire() -> None:
    # The example was `web_search` until that became a REAL tool - the fate of every good
    # fictional tool name. The doctrine now (connector tool names are open
    # vocabulary): an unregistered name passes through raw (honesty), its arg KEYS label the
    # call (the connector journal's own keys-only format), and its VALUES never appear.
    from pydantic_ai.messages import ToolCallPart as TCP

    sink: list[LiveEvent] = []
    tap = MessageTap(_deps(sink), "writer", flush_s=0.0)
    await _drive(
        tap,
        [
            FunctionToolCallEvent(
                part=TCP(
                    tool_name="teleport_user",
                    args={"query": "secret internal codename"},
                    tool_call_id="tc1",
                )
            ),
        ],
    )
    start = _acts(sink, "tool")[0]
    assert getattr(start, "tool", "") == "teleport_user"  # the NAME is visible - honesty
    assert getattr(start, "label", "x") == "args: query"  # the KEYS label it - parity
    assert "codename" not in str(sink)  # the values never do - safety


async def test_two_text_parts_read_as_paragraphs_of_one_message() -> None:
    sink: list[LiveEvent] = []
    tap = MessageTap(_deps(sink), "writer", flush_s=0.0)

    await _drive(
        tap,
        [
            PartStartEvent(index=0, part=TextPart(content="First look.")),
            PartStartEvent(index=2, part=TextPart(content="After the tool: the answer.")),
        ],
    )

    assert [e.type for e in sink].count("message.start") == 1
    assert [e.type for e in sink].count("message.end") == 1
    text = "".join(e.text for e in sink if e.type == "message.delta")
    assert text == "First look.\n\nAfter the tool: the answer."


async def test_long_text_is_sliced_under_the_frame_ceiling() -> None:
    sink: list[LiveEvent] = []
    tap = MessageTap(_deps(sink), "writer", flush_s=0.0)

    long = "x" * (MESSAGE_SLICE_CHARS * 2 + 100)
    await _drive(tap, [PartStartEvent(index=0, part=TextPart(content=long))])

    deltas = [e for e in sink if e.type == "message.delta"]
    assert all(len(e.text) <= MESSAGE_SLICE_CHARS for e in deltas)
    assert "".join(e.text for e in deltas) == long


async def test_a_typed_collection_output_pours_as_the_draft() -> None:
    """The organizer's deliverable streams as the output tool's raw JSON args - the
    counting card only tallies rows, so verbatim fragments are exactly enough signal."""
    from pydantic_ai.messages import ToolCallPart, ToolCallPartDelta

    sink: list[LiveEvent] = []
    tap = MessageTap(_deps(sink), "maker", flush_s=0.0, pour_output=True)

    args = '{"filename": "tracker", "summary": "…", "collection": {"rows": [{"id": "r1"}, {"id": "r2"}]}}'
    await _drive(
        tap,
        [
            PartStartEvent(
                index=0, part=ToolCallPart(tool_name="final_result", args=args[:30], tool_call_id="tc1")
            ),
            PartDeltaEvent(index=0, delta=ToolCallPartDelta(args_delta=args[30:], tool_call_id="tc1")),
        ],
    )

    drafts = [e for e in sink if e.type == "artifact.delta"]
    assert drafts, "the typed output must stream a draft"
    assert all(d.path == "/work/draft.collection.json" for d in drafts)
    assert "".join(d.text for d in drafts) == args, "raw args, verbatim, in order"


async def test_a_text_deliverables_prose_pours_as_the_draft_not_the_message() -> None:
    """The writer's ANSWER is the document - its TextParts stream on the
    draft channel (`/work/draft.md`) and the message lane stays silent, so the document
    composes in the artifact pane instead of arriving as a chat bubble that then becomes a
    card. Parts across tool rounds join as paragraphs of ONE document."""
    sink: list[LiveEvent] = []
    tap = MessageTap(_deps(sink), "writer", flush_s=0.0, text_as_draft=True)

    await _drive(
        tap,
        [
            PartStartEvent(index=0, part=TextPart(content="# Theory\n\nThe claim.")),
            PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=" More of it.")),
            PartStartEvent(index=2, part=TextPart(content="A second round's text.")),
        ],
    )

    assert not [e for e in sink if e.type.startswith("message.")], "the message lane stays silent"
    drafts = [e for e in sink if e.type == "artifact.delta"]
    assert drafts and all(d.path == "/work/draft.md" for d in drafts)
    assert "".join(d.text for d in drafts) == "# Theory\n\nThe claim. More of it.\n\nA second round's text."


async def test_a_restart_ends_the_message_and_the_revised_one_starts_fresh() -> None:
    """A final-answer redirect: the answer so far drains and ends, and the revised
    answer is a NEW message - a correction is a new message, never a rewrite."""
    sink: list[LiveEvent] = []
    tap = MessageTap(_deps(sink), "maker", flush_s=60.0)  # nothing flushes on cadence: restart drains

    await _invoke(tap, [PartStartEvent(index=0, part=TextPart(content="It is $100."))])
    await tap.restart()
    await _drive(tap, [PartStartEvent(index=0, part=TextPart(content="It is €92."))])

    lane = [e for e in sink if e.type.startswith("message.")]
    assert [e.type for e in lane] == [
        "message.start",
        "message.delta",
        "message.end",
        "message.start",
        "message.delta",
        "message.end",
    ]
    assert [e.text for e in lane if e.type == "message.delta"] == ["It is $100.", "It is €92."]


async def test_a_restart_pours_the_revised_document_at_a_new_draft_path() -> None:
    """The writer's document after a redirect is a fresh draft: a new path, so the web (which
    keys a draft on its path) starts it from nothing instead of overlaying the old one."""
    sink: list[LiveEvent] = []
    tap = MessageTap(_deps(sink), "writer", flush_s=0.0, text_as_draft=True)

    await _invoke(tap, [PartStartEvent(index=0, part=TextPart(content="# In dollars"))])
    await tap.restart()
    await _drive(tap, [PartStartEvent(index=0, part=TextPart(content="# In euros"))])

    drafts = [e for e in sink if isinstance(e, ArtifactDelta)]
    assert [(d.path, d.offset, d.text) for d in drafts] == [
        ("/work/draft.md", 0, "# In dollars"),
        ("/work/revision-2/draft.md", 0, "# In euros"),
    ]
    assert not [e for e in sink if e.type.startswith("message.")], "the message lane stays silent"


async def test_a_plain_agents_final_result_stays_off_the_draft_lane() -> None:
    """Only typed-deliverable agents pour their output - a researcher's findings JSON is not
    a document draft."""
    from pydantic_ai.messages import ToolCallPart

    sink: list[LiveEvent] = []
    tap = MessageTap(_deps(sink), "researcher", flush_s=0.0)  # pour_output defaults False

    await _drive(
        tap,
        [
            PartStartEvent(
                index=0,
                part=ToolCallPart(tool_name="final_result", args='{"findings": []}', tool_call_id="tc1"),
            ),
        ],
    )
    assert not [e for e in sink if e.type == "artifact.delta"]


async def test_a_quiet_seat_pours_prose_to_the_tree_with_paragraph_seams() -> None:
    """The quiet lane: a fan-out seat's prose lights the tree as thinking frames -
    no message frames - and a new text part joins the pour as a new paragraph, the same
    seam the message and text-as-draft lanes insert."""
    sink: list[LiveEvent] = []
    tap = MessageTap(_deps(sink), "researcher", flush_s=0.0, quiet_stream=True)

    await _drive(
        tap,
        [
            PartStartEvent(index=0, part=TextPart(content="Scanning the fence.")),
            PartStartEvent(index=1, part=TextPart(content="Two records found.")),
        ],
    )

    assert not [e for e in sink if e.type.startswith("message.")], "quiet means quiet"
    poured = "".join(getattr(e, "text", "") or "" for e in _acts(sink, "thinking"))
    assert poured == "Scanning the fence.\n\nTwo records found."
