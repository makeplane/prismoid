# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The streamed draft - the answer recovered from `write_file`'s argument JSON as it arrives.

Every scanner test feeds fragments at hostile split points (mid-escape, mid-surrogate, mid-key),
because that is exactly how provider deltas arrive; the last one drives the real tap and pins
what reaches the draft lane.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic_ai.messages import (
    AgentStreamEvent,
    PartDeltaEvent,
    PartStartEvent,
    ToolCallPart,
    ToolCallPartDelta,
)
from pydantic_core import from_json

from prismoid.ai.agents import draft
from prismoid.ai.agents.draft import PARSE_EVERY_S, DraftScanner
from prismoid.ai.agents.tap import MessageTap
from prismoid.ai.deps import TaskDeps
from prismoid.schemas.conversation import ChatLiveEvent
from prismoid.schemas.events import ArtifactDelta, LiveEvent
from prismoid.schemas.ids import StepId, TaskId


def fed(scanner: DraftScanner, fragment: str) -> str:
    """One fragment, then a parse: what the tap sees when that fragment ends a response."""
    return scanner.feed(fragment) + scanner.finish()


def drain(scanner: DraftScanner, fragments: list[str]) -> str:
    return "".join(fed(scanner, f) for f in fragments)


def test_content_streams_out_as_it_arrives() -> None:
    s = DraftScanner()
    assert fed(s, '{"path": "/work/a.md", "content": "Hel') == "Hel"
    assert fed(s, "lo wor") == "lo wor"
    assert fed(s, 'ld"}') == "ld"
    assert s.path == "/work/a.md"


def test_content_before_path_still_captures_both() -> None:
    s = DraftScanner()
    assert fed(s, '{"content": "abc", "path": "/wo') == "abc"
    assert s.path is None, "a path still arriving is never named"
    assert fed(s, 'rk/x.md"}') == ""
    assert s.path == "/work/x.md"


def test_escapes_split_across_fragments() -> None:
    s = DraftScanner()
    out = drain(s, ['{"content": "line1\\', 'nline2 \\"quoted\\', '" tab\\t!"}'])
    assert out == 'line1\nline2 "quoted" tab\t!'


def test_unicode_escape_split_mid_hex() -> None:
    s = DraftScanner()
    assert fed(s, '{"content": "caf\\u00') == "caf"
    assert fed(s, 'e9!"}') == "é!"


def test_surrogate_pair_emoji_split_between_halves() -> None:
    s = DraftScanner()
    out = drain(s, ['{"content": "hi \\ud83d', '\\ude00 there"}'])
    assert out == "hi 😀 there"


def test_every_split_point_of_a_real_payload() -> None:
    payload = json.dumps({"path": "/work/doc.md", "content": 'A "quoted" line\nwith\ttabs and café 😀'})
    expected = 'A "quoted" line\nwith\ttabs and café 😀'
    for cut in range(1, len(payload)):
        s = DraftScanner()
        out = fed(s, payload[:cut]) + fed(s, payload[cut:])
        assert out == expected, f"split at {cut}"
        assert s.path == "/work/doc.md"


def test_invalid_json_stops_the_draft_never_the_run() -> None:
    s = DraftScanner()
    assert fed(s, '{"content": "bad ') == "bad "
    assert fed(s, '\\ud83d end"}') == ""
    assert fed(s, "more") == ""


def test_nested_content_streams_nothing() -> None:
    s = DraftScanner()
    assert drain(s, ['{"content": {"nested": true}}']) == ""


def test_non_string_values_are_skipped() -> None:
    s = DraftScanner()
    assert drain(s, ['{"count": 42, "flag": true, "content": "ok"}']) == "ok"


def test_other_tools_flat_args_yield_nothing() -> None:
    s = DraftScanner()
    assert drain(s, ['{"command": "python3 /work/build.py"}']) == ""
    assert s.path is None


class FakeClock:
    """Time that moves only when the test says so."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_a_fast_stream_parses_at_most_four_times_a_second() -> None:
    clock = FakeClock()
    s = DraftScanner(clock=clock)
    assert s.feed('{"path": "/work/a.md", "content": "Hel') == "Hel", "the first fragment parses at once"
    clock.now = 0.1
    assert s.feed("lo") == "", "too soon: the fragment waits"
    clock.now = 0.3
    assert s.feed(" there") == "lo there"
    assert s.feed('"}') == ""
    assert s.finish() == ""
    assert s.path == "/work/a.md"


def test_a_long_write_parses_once_per_interval_and_ends_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    parses: list[int] = []

    def counted(data: str, **kwargs: Any) -> Any:
        if kwargs.get("allow_partial") == "trailing-strings":
            parses.append(len(data))
        return from_json(data, **kwargs)

    monkeypatch.setattr(draft, "from_json", counted)
    content = ('A line of prose with café and "quotes".\n' * 25_000)[:1_000_000]
    payload = json.dumps({"path": "/work/big.md", "content": content})
    clock = FakeClock()
    s = DraftScanner(clock=clock)
    out = ""
    for at in range(0, len(payload), 20):
        clock.now += 0.001  # a 20-character delta every millisecond
        out += s.feed(payload[at : at + 20])
    out += s.finish()
    assert out == content
    assert len(parses) <= clock.now / PARSE_EVERY_S + 2, (
        "one parse per interval, plus the first and the final"
    )


async def test_a_write_file_call_pours_its_content_to_the_draft_lane() -> None:
    sink: list[LiveEvent] = []
    clock = FakeClock()

    async def emit(event: LiveEvent | ChatLiveEvent) -> None:
        sink.append(event)  # pyright: ignore[reportArgumentType]

    tap = MessageTap(
        TaskDeps(task_id=TaskId(uuid.uuid4()), step_id=StepId(uuid.uuid4()), emit_event=emit),
        "writer",
        flush_s=0.0,
        draft_clock=clock,
    )
    body = "# Brief\n\n" + 'Café "prices" 😀 rose.\n' * 200
    args = json.dumps({"path": "/work/brief.md", "content": body})

    async def stream() -> AsyncIterator[AgentStreamEvent]:
        yield PartStartEvent(
            index=1, part=ToolCallPart(tool_name="write_file", args=args[:7], tool_call_id="w1")
        )
        for at in range(7, len(args), 5):
            clock.now += 0.1  # a parse every few deltas, mid-stream, whatever the machine's load
            yield PartDeltaEvent(
                index=1, delta=ToolCallPartDelta(args_delta=args[at : at + 5], tool_call_id="w1")
            )

    await tap(None, stream())
    await tap.close()

    drafts = [e for e in sink if isinstance(e, ArtifactDelta)]
    assert drafts, "the response's end parses the rest of the call"
    assert all(d.path == "/work/brief.md" for d in drafts)
    assert [d.offset for d in drafts] == [sum(len(p.text) for p in drafts[:i]) for i in range(len(drafts))]
    assert "".join(d.text for d in drafts) == body
