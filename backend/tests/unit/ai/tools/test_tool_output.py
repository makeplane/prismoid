# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a frame tool hands the model: a non-zero shell exit is a normal result (the exit code and
the output, stderr first), long output keeps its head AND its tail with the whole of it spilled to
a file under /work, `read_file` reads a long file in numbered windows that say where to read on, and frame
tools never run concurrently."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from prismoid.ai.frames.driver import ExecResult
from prismoid.ai.frames.layout import STANDARD_PATHS
from prismoid.ai.tools.frame_tools import PUBLISH, frame_toolset
from prismoid.ai.tools.output import OUTPUT_CAP, capped


@dataclass
class _Frame:
    """A cloud frame whose `shell` answers `answer`, whose files are `files`, and which counts how
    many calls are inside it at once."""

    answer: ExecResult = field(default_factory=lambda: ExecResult(0, "", ""))
    files: dict[str, str] = field(default_factory=dict[str, str])
    paths = STANDARD_PATHS
    inside: int = 0
    most_inside: int = 0

    async def _enter(self) -> None:
        self.inside += 1
        self.most_inside = max(self.most_inside, self.inside)
        await asyncio.sleep(0.01)
        self.inside -= 1

    async def write_file(self, path: str, content: str) -> None:
        await self._enter()
        self.files[path] = content

    async def write_bytes(self, path: str, data: bytes) -> None: ...

    async def read_file(self, path: str) -> str:
        await self._enter()
        return self.files[path]

    async def read_bytes(self, path: str) -> bytes:
        return b""

    async def exec(self, command: str, timeout: float = 120.0) -> Any:  # noqa: ASYNC109
        if command.startswith("realpath -m -- "):
            return ExecResult(0, command.removeprefix("realpath -m -- ").strip("'") + "\n", "")
        await self._enter()
        return self.answer

    async def publish(self, path: str, type: str, title: str, sources: Any = ()) -> None: ...


class _Ctx:
    tool_call_id = "toolu_01"


def _shell(frame: _Frame) -> Any:
    return frame_toolset(("frame.shell", "frame.write_file"), frame).tools["shell"].function


async def test_a_non_zero_exit_is_a_normal_result_with_stderr_first() -> None:
    """A failed command is the model's to read and fix: a `ModelRetry` is spent from the run's
    retries, so three failed builds in a row killed the attempt."""
    frame = _Frame(answer=ExecResult(2, "partial output\n", "SyntaxError: bad line 3\n"))

    said = await _shell(frame)(_Ctx(), "python3 /work/build.py")

    assert said.startswith("[exit 2]")
    assert said.index("SyntaxError") < said.index("partial output"), "stderr first: the reason leads"


async def test_a_quiet_success_says_so() -> None:
    assert await _shell(_Frame())(_Ctx(), "true") == "(exit 0, no output)"


async def test_long_output_keeps_head_and_tail_and_spills_the_whole() -> None:
    """The error a build prints is at the END: a head-only cut hid the one line that mattered."""
    body = "\n".join(f"line {n}" for n in range(5000))
    frame = _Frame(answer=ExecResult(1, body + "\nFATAL: the last line\n", ""))

    said = await _shell(frame)(_Ctx(), "make")

    assert len(said) < OUTPUT_CAP + 400
    assert "line 0" in said and "FATAL: the last line" in said
    (spilled,) = frame.files
    assert spilled.startswith("/work/") and spilled in said, "the result names the file"
    assert "FATAL: the last line" in frame.files[spilled] and "line 2500" in frame.files[spilled]


async def test_a_long_file_is_read_in_numbered_windows_that_say_how_to_read_on() -> None:
    body = "\n".join(f"line {n}" for n in range(1, 5001))
    frame = _Frame(files={"/work/big.log": body})
    read: Any = frame_toolset(("frame.read_file",), frame).tools["read_file"].function

    said = await read(_Ctx(), "/work/big.log")

    assert len(said) < OUTPUT_CAP + 400
    assert said.startswith("     1\tline 1\n") and "offset=" in said and "of 5000" in said


async def test_the_middle_of_a_long_file_is_read_with_read_file_itself() -> None:
    """The researcher, the writer and the architect read files and hold no shell: a cut that sent
    them to `sed -n` left the middle of a long source out of reach. A window names the offset to read
    on from, and a read from it hands back the next lines, numbered, to the end."""
    body = "\n".join(f"<{n:05d}>" for n in range(1, OUTPUT_CAP))
    frame = _Frame(files={"/task/inputs/report.md": body})
    read: Any = frame_toolset(("frame.read_file",), frame).tools["read_file"].function

    first = await read(_Ctx(), "/task/inputs/report.md")
    assert "offset=" in first and "shell" not in first

    middle = await read(_Ctx(), "/task/inputs/report.md", offset=6000)
    assert middle.startswith("  6000\t<06000>") and len(middle) < OUTPUT_CAP + 400

    last = await read(_Ctx(), "/task/inputs/report.md", offset=OUTPUT_CAP - 3)
    assert last.startswith(f"{OUTPUT_CAP - 3:6}\t") and "end of the file" in last


def test_short_text_is_untouched() -> None:
    assert capped("hello") == "hello"


async def test_frame_tools_never_run_concurrently() -> None:
    """One sandbox, one working directory: two parallel calls (a write and a build that reads it)
    raced each other. Frame tools run one at a time, like the browser's."""
    frame = _Frame(files={"/work/a.md": "a"})
    toolset = frame_toolset(("frame.shell", "frame.read_file", "frame.write_file"), frame)
    calls = [
        ToolCallPart("shell", {"command": "ls"}, tool_call_id="c1"),
        ToolCallPart("read_file", {"path": "/work/a.md"}, tool_call_id="c2"),
        ToolCallPart("write_file", {"path": "/work/b.md", "content": "b"}, tool_call_id="c3"),
    ]

    async def model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        if any(isinstance(p, ToolReturnPart) for p in messages[-1].parts):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=list(calls))

    await Agent(FunctionModel(model), toolsets=[toolset]).run("go")

    assert frame.most_inside == 1


def test_publish_is_offered_by_the_name_the_run_guard_lets_through() -> None:
    """A guarded run told to finish may still call `PUBLISH`: the toolset must give it that name."""
    assert PUBLISH in frame_toolset(("frame.publish",), _Frame()).tools


@dataclass
class _DocumentFrame(_Frame):
    """A frame holding binary files, as a scatter seat's frame holds a person's attachments."""

    blobs: dict[str, bytes] = field(default_factory=dict[str, bytes])

    async def read_bytes(self, path: str) -> bytes:
        return self.blobs[path]


def _workbook(rows: int) -> bytes:
    import io

    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.append(["region", "units"])
    for n in range(1, rows + 1):
        sheet.append([f"region-{n}", n])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


async def test_a_seat_without_a_shell_reads_a_spreadsheet_as_text() -> None:
    """An agent that holds `read_file` and no shell reads an xlsx through the conversion sidecar: every
    row, numbered and paged like any text file."""
    frame = _DocumentFrame(blobs={"/task/inputs/sales.xlsx": _workbook(200)})
    read: Any = frame_toolset(("frame.read_file",), frame).tools["read_file"].function

    said = await read(_Ctx(), "/task/inputs/sales.xlsx")

    assert "region-1 " in said and "region-200" in said and "binary" not in said


async def test_a_document_the_sidecar_cannot_read_is_an_answer_not_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from prismoid.ai.tools import convert

    async def timed_out(*_args: Any, **_kwargs: Any) -> convert.Converted:
        raise convert.ConvertFailed("could not read the .pdf - converting it timed out")

    monkeypatch.setattr(convert, "convert", timed_out)
    frame = _DocumentFrame(blobs={"/task/inputs/broken.pdf": b"%PDF-1.7"})
    read: Any = frame_toolset(("frame.read_file",), frame).tools["read_file"].function

    said = await read(_Ctx(), "/task/inputs/broken.pdf")

    assert said.startswith("/task/inputs/broken.pdf: could not read the .pdf") and "Do not retry" in said
