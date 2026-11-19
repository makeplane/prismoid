# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Write the parked-history fixture: a step's message history exactly as a park stored it.

A step parked before the call log kept its pydantic-ai message history as JSON bytes, decoded latin-1 into the
step's `resume_state`; the resume decodes it (`schemas/park.V1Park`) and loads it
(`operations/execution/run.py`). That shape belongs to the pydantic-ai version that wrote it, and a
park can outlive an upgrade. This script freezes one park, written by the version installed now, so a
test can prove a later version still loads and resumes it. Run it only before a pydantic-ai upgrade,
never after: the fixture's value is that an older version wrote it.

The history holds every part an upgrade is likely to reshape: a signed thinking part, a retry with a
string reason, a retry with a validation-error list, a retry tied to no tool, a tool return carrying a
PNG, and an `ask_user` call left unanswered, which is the park itself. Every timestamp and id is fixed,
so two runs write the same bytes.

    uv run python tools/gen_parked_history.py           # from backend/
"""

from __future__ import annotations

import json
import pathlib
import struct
import sys
import zlib
from datetime import UTC, datetime
from importlib.metadata import version

from pydantic import BaseModel, ValidationError
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage
from pydantic_core import ErrorDetails

OUT = pathlib.Path("tests/fixtures/pydantic_ai/parked_history_v2_53.json")
WRITER = "2.53"  # the version this fixture must be written by; its name says so too

AT = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
MODEL = "fixture-model"  # a stand-in: the history is replayed, never sent to a provider


def _png() -> bytes:
    """A real 1x1 PNG, built here so its bytes are known: a screenshot's shape without its size."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)  # 1x1, 8-bit RGBA
    pixels = zlib.compress(b"\x00\x00\x00\x00\x00", 9)  # one row: no filter, one transparent pixel
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


class _Click(BaseModel):
    ref: str


def _click_errors() -> list[ErrorDetails]:
    """The validation errors pydantic itself reports for a click whose `ref` is a number."""
    try:
        _Click.model_validate({"ref": 42})
    except ValidationError as error:
        return list(error.errors(include_url=False))
    raise AssertionError("a numeric ref must not validate")


def _response(*parts: ThinkingPart | TextPart | ToolCallPart) -> ModelResponse:
    return ModelResponse(
        parts=list(parts),
        usage=RequestUsage(input_tokens=100, output_tokens=20),
        model_name=MODEL,
        timestamp=AT,
        provider_name="anthropic",
        provider_response_id="msg_fixture",
        finish_reason="tool_call",
    )


def history() -> list[ModelMessage]:
    return [
        ModelRequest(
            parts=[
                UserPromptPart(
                    content="Book a table for two at Café Lumière ☕, and ask me the time.", timestamp=AT
                )
            ],
            timestamp=AT,
        ),
        _response(
            ThinkingPart(
                content="I should open the restaurant's site first.",
                signature="sig-fixture-0001",
                provider_name="anthropic",
            ),
            ToolCallPart(
                tool_name="browser_open", args={"url": "https://cafe-lumiere.example"}, tool_call_id="c1"
            ),
        ),
        ModelRequest(
            parts=[
                RetryPromptPart(
                    content="The page did not load: connection reset. Try again.",
                    tool_name="browser_open",
                    tool_call_id="c1",
                    timestamp=AT,
                )
            ],
            timestamp=AT,
        ),
        _response(ToolCallPart(tool_name="browser_click", args={"ref": 42}, tool_call_id="c2")),
        ModelRequest(
            parts=[
                RetryPromptPart(
                    content=_click_errors(),
                    tool_name="browser_click",
                    tool_call_id="c2",
                    timestamp=AT,
                )
            ],
            timestamp=AT,
        ),
        _response(TextPart(content="{bad json")),
        ModelRequest(
            parts=[
                RetryPromptPart(
                    content="Please return a valid answer or call a tool.",
                    tool_name=None,
                    tool_call_id="r1",
                    timestamp=AT,
                )
            ],
            timestamp=AT,
        ),
        _response(ToolCallPart(tool_name="browser_screenshot", args={}, tool_call_id="c3")),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="browser_screenshot",
                    content=BinaryContent(data=_png(), media_type="image/png"),
                    tool_call_id="c3",
                    timestamp=AT,
                )
            ],
            timestamp=AT,
        ),
        _response(
            ToolCallPart(
                tool_name="ask_user",
                args={"questions": [{"question": "What time should I book?", "options": []}]},
                tool_call_id="q1",
            )
        ),
    ]


def main() -> None:
    installed = version("pydantic-ai-slim")
    if not installed.startswith(f"{WRITER}."):
        sys.exit(f"pydantic-ai-slim {installed} is installed; this fixture must be written by {WRITER}.x")
    messages = history()
    raw = ModelMessagesTypeAdapter.dump_json(messages)
    stored = raw.decode("latin-1")  # what the park writes into resume_state
    # Bytes, not objects: loading narrows BinaryContent to BinaryImage, which dumps the same.
    if (
        stored.encode("latin-1") != raw
        or ModelMessagesTypeAdapter.dump_json(
            ModelMessagesTypeAdapter.validate_json(stored.encode("latin-1"))
        )
        != raw
    ):
        sys.exit("the history does not survive the park's latin-1 round trip")
    record = {"history": stored, "tool_call_id": "q1", "written_by": f"pydantic-ai-slim {installed}"}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="ascii")
    print(f"  {len(messages)} messages, {len(raw)} bytes, pydantic-ai-slim {installed} -> {OUT}")


if __name__ == "__main__":
    main()
