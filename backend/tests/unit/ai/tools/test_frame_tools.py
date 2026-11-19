# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""read_file's three shapes (the IMG_8038 failure): an image is read as the
image itself, mangled binary text is refused as a ModelRetry - never returned as context
poison - and honest text passes untouched."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar

import pytest
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from prismoid.ai.frames.driver import ExecResult
from prismoid.ai.frames.layout import STANDARD_PATHS, OutsideFrame
from prismoid.ai.tools.frame_tools import frame_toolset, image_media_for, refuse_mangled_binary
from tests.support.runners import run_recorded


def test_images_are_seen_not_catted() -> None:
    assert image_media_for("/task/inputs/IMG_8038.JPG") == "image/jpeg"
    assert image_media_for("/work/chart.png") == "image/png"
    assert image_media_for("/task/inputs/brief.md") is None
    assert image_media_for("/work/report.xlsx") is None


def test_mangled_binary_is_refused_not_returned() -> None:
    catted_xlsx = "PK\x03\x04" + "�" * 40 + "sheet1"
    with pytest.raises(ModelRetry):
        refuse_mangled_binary("/task/inputs/data.xlsx", catted_xlsx)
    with pytest.raises(ModelRetry):
        refuse_mangled_binary("/work/blob", "x\x00y")


def test_honest_text_passes() -> None:
    refuse_mangled_binary("/task/inputs/memo.md", "# Q3 memo\nRevenue rose 12%.\n")
    # a stray replacement char (mojibake in a pasted doc) is not a binary
    refuse_mangled_binary("/task/inputs/notes.txt", "caf� latte")


# ── the recording is the engine's evidence, never an agent's ───────────────────────────────────


@dataclass
class _Frame:
    published: list[tuple[str, str, str, tuple[str, ...]]] = field(default_factory=list)
    paths = STANDARD_PATHS
    # Symlinks the agent made in the frame, link -> target; `realpath -m` follows them.
    links: dict[str, str] = field(default_factory=dict)
    execs: list[str] = field(default_factory=list)

    async def write_file(self, path: str, content: str) -> None: ...
    async def write_bytes(self, path: str, data: bytes) -> None: ...
    async def read_file(self, path: str) -> str:
        return ""

    async def read_bytes(self, path: str) -> bytes:
        return b""

    async def exec(self, command: str, timeout: float = 120.0) -> Any:  # noqa: ASYNC109
        self.execs.append(command)
        argv = shlex.split(command)
        if argv[:3] != ["realpath", "-m", "--"]:
            return ExecResult(127, "", f"{argv[0]}: not faked")
        path = argv[3]
        for _ in range(8):  # a link to a link, as the kernel would walk it
            link = next((k for k in self.links if path == k or path.startswith(f"{k}/")), None)
            if link is None:
                break
            path = self.links[link] + path[len(link) :]
        return ExecResult(0, f"{path}\n", "")

    async def publish(self, path: str, type: str, title: str, sources: Any = ()) -> None:
        self.published.append((path, type, title, tuple(sources)))


@dataclass
class _Journal:
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(
        self,
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        ref: str | None = None,
    ) -> None:
        self.calls.append({"tool": tool, "ok": ok, "summary": summary, "denied": denied_reason})


class _Ctx:
    tool_call_id = "toolu_publish_01"


class _Refusing(_Frame):
    """A frame that refuses every file: `paths` says where it is."""

    def __init__(self, on_host: bool) -> None:
        super().__init__()
        self.paths = replace(STANDARD_PATHS, on_host=on_host)

    async def write_file(self, path: str, content: str) -> None:
        raise PermissionError(f"{path}: protected")

    async def read_file(self, path: str) -> str:
        raise OutsideFrame("that path is in none of this task's folders")

    async def read_bytes(self, path: str) -> bytes:
        raise PermissionError(f"{path}: protected")


async def test_a_refusal_on_the_users_disk_is_the_models_to_read() -> None:
    journal = _Journal()
    toolset = frame_toolset(("frame.write_file", "frame.read_file"), _Refusing(on_host=True), journal)
    write: Any = toolset.tools["write_file"].function
    read: Any = toolset.tools["read_file"].function
    assert "protected" in await write(_Ctx(), "/work/.env", "x")
    assert "outside the files you can read" in await read(_Ctx(), "/Users/me/x")
    assert "protected" in await read(_Ctx(), "/work/photo.png")
    assert [c["ok"] for c in journal.calls] == [False, False, False], "still journalled as refused"


async def test_refusals_never_count_against_the_retries() -> None:
    """On the file door: a `ModelRetry` is spent from `retries=3`, so the fourth
    refusal in a row used to end the run and buy the step a fresh attempt."""
    refused = [("read_file", {"path": f"/Users/me/.ssh/key{n}"}) for n in range(5)]
    refused += [("read_file", {"path": f"/Users/me/photo{n}.png"}) for n in range(5)]
    refused += [("write_file", {"path": f"/work/.git/hooks/h{n}", "content": "x"}) for n in range(5)]
    turns = iter(refused)
    answers: list[str] = []

    async def model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        answers.extend(
            str(part.content)
            for part in messages[-1].parts
            if isinstance(part, ToolReturnPart)  # a refusal sent back is a retried return
        )
        if (turn := next(turns, None)) is None:
            return ModelResponse(parts=[TextPart("I could not reach those files.")])
        return ModelResponse(parts=[ToolCallPart(*turn)])

    toolset = frame_toolset(("frame.write_file", "frame.read_file"), _Refusing(on_host=True), _Journal())
    agent = Agent(FunctionModel(model), toolsets=[toolset], retries=3)

    result = await agent.run("read my keys")

    assert result.output == "I could not reach those files."
    assert len(answers) == len(refused)
    assert all("Refused" in answer for answer in answers)


async def test_a_cloud_frames_errors_are_as_they_were() -> None:
    toolset = frame_toolset(("frame.write_file", "frame.read_file"), _Refusing(on_host=False), _Journal())
    write: Any = toolset.tools["write_file"].function
    read: Any = toolset.tools["read_file"].function
    with pytest.raises(PermissionError):
        await write(_Ctx(), "/work/.env", "x")
    with pytest.raises(OutsideFrame):
        await read(_Ctx(), "/work/x")  # inside the roots, so the driver answers


async def test_publish_refuses_the_recording_type_before_the_frame_ever_sees_it() -> None:
    frame, journal = _Frame(), _Journal()
    toolset = frame_toolset(("frame.publish",), frame, journal)
    publish: Any = toolset.tools["publish"].function

    with pytest.raises(ModelRetry, match="engine, never by an agent"):
        await publish(_Ctx(), "/work/recording-9.tar", "recording", "Recording")
    args = {"path": "/work/recording-9.tar", "type": "recording", "title": "Recording"}
    await run_recorded(toolset, [("publish", args)], journal, frame=frame)

    assert frame.published == [], "no bytes moved and no manifest entry was written for the forgery"
    denied = [c for c in journal.calls if not c["ok"]]
    assert denied and denied[0]["tool"] == "frame.publish"
    assert "engine" in denied[0]["denied"]


async def test_publish_still_accepts_an_ordinary_type() -> None:
    frame, journal = _Frame(), _Journal()
    toolset = frame_toolset(("frame.publish",), frame, journal)
    publish: Any = toolset.tools["publish"].function

    result = await publish(_Ctx(), "/work/report.md", "document", "Report")

    assert result == "published /work/report.md as document"
    assert frame.published == [("/work/report.md", "document", "Report", ())]


# ── write_file lands only where an agent may write ─────────────────────────────────────────────


class _Recording(_Frame):
    def __init__(self, paths: Any = STANDARD_PATHS) -> None:
        super().__init__()
        self.paths = paths
        self.written: list[str] = []

    async def write_file(self, path: str, content: str) -> None:
        self.written.append(path)


@pytest.mark.parametrize(
    "outside",
    [
        "/opt/plane/browser/sidecar/ctl.py",  # the sidecar's code, run as the browser user
        "/browser/profile/Cookies",
        "/task/answers.json",
        "/task/inputs/brief.md",
        "/verify/suite.py",
        "/skills/x/SKILL.md",
        "/work/../task/answers.json",
        "/artifacts/../etc/passwd",
        "../verify/suite.py",
        "/tmp/build.py",
    ],
)
async def test_write_file_refuses_every_path_outside_work_and_artifacts(outside: str) -> None:
    """A driver writes as root (`put_archive`, a remote driver's file door), so the uid split cannot be
    what keeps an agent out of the sidecar's code or `/task`: the path is confined at the tool,
    normalized first, and a refusal is the model's to read, never a retry."""
    frame, journal = _Recording(), _Journal()
    write: Any = frame_toolset(("frame.write_file",), frame, journal).tools["write_file"].function
    result = await write(_Ctx(), outside, "payload")
    assert result.startswith("Refused:") and "/work" in result
    assert frame.written == []
    assert [c["ok"] for c in journal.calls] == [False], "a refusal is journalled"


async def test_write_file_lands_in_work_and_artifacts() -> None:
    frame = _Recording()
    write: Any = frame_toolset(("frame.write_file",), frame, _Journal()).tools["write_file"].function
    for path in ("/work/./report.md", "notes/a.md", "/artifacts/out.csv", "/work/sub/../b.py"):
        assert (await write(_Ctx(), path, "x")).startswith("wrote ")
    assert frame.written == ["/work/report.md", "/work/notes/a.md", "/artifacts/out.csv", "/work/b.py"]


@pytest.mark.parametrize(
    ("links", "path"),
    [
        ({"/work/t": "/task"}, "/work/t/context.md"),  # a symlinked parent
        ({"/work/notes.md": "/verify/suite.py"}, "/work/notes.md"),  # a symlinked file
        ({"/work/a": "/work/b", "/work/b": "/opt/plane/browser"}, "/work/a/ctl.py"),  # a chain
        ({"/artifacts/x": "/browser/profile"}, "/artifacts/x/Cookies"),
    ],
)
async def test_a_symlink_the_agent_made_cannot_carry_a_write_out_of_work(
    links: dict[str, str], path: str
) -> None:
    """A cloud driver writes as root and follows a symlink inside the frame (Docker's
    `put_archive`, sandbar's file write), so a textual check alone would let `/work/t -> /task`
    land a write in `/task`. The target is resolved in the frame and confined again."""
    frame, journal = _Recording(), _Journal()
    frame.links = links
    write: Any = frame_toolset(("frame.write_file",), frame, journal).tools["write_file"].function
    result = await write(_Ctx(), path, "payload")
    assert result.startswith("Refused:")
    assert frame.written == []


async def test_a_symlink_inside_work_writes_where_it_points() -> None:
    frame = _Recording()
    frame.links = {"/work/latest": "/work/runs/3"}
    write: Any = frame_toolset(("frame.write_file",), frame, _Journal()).tools["write_file"].function
    assert (await write(_Ctx(), "/work/latest/out.md", "x")).startswith("wrote ")
    assert frame.written == ["/work/runs/3/out.md"]


async def test_a_write_whose_target_cannot_be_resolved_is_refused() -> None:
    class _NoRealpath(_Recording):
        async def exec(self, command: str, timeout: float = 120.0) -> Any:  # noqa: ASYNC109
            return ExecResult(1, "", "realpath: missing")

    frame = _NoRealpath()
    write: Any = frame_toolset(("frame.write_file",), frame, _Journal()).tools["write_file"].function
    assert (await write(_Ctx(), "/work/report.md", "x")).startswith("Refused:")
    assert frame.written == []


async def test_a_device_frame_writes_its_granted_folders_and_nothing_else() -> None:
    paths = replace(
        STANDARD_PATHS,
        on_host=True,
        work="/Users/me/Prismoid/t1/work",
        task="/Users/me/Prismoid/t1/task",
        grants=(("/grants/proj", "/Users/me/proj"),),
    )
    frame = _Recording(paths)
    write: Any = frame_toolset(("frame.write_file",), frame, _Journal()).tools["write_file"].function
    for path in ("/Users/me/proj/app.py", "/Users/me/Prismoid/t1/work/x.md", "/work/y.md"):
        assert (await write(_Ctx(), path, "x")).startswith("wrote ")
    for path in ("/Users/me/.ssh/authorized_keys", "/Users/me/Prismoid/t1/task/answers.json"):
        assert (await write(_Ctx(), path, "x")).startswith("Refused:")
    assert frame.written == ["/grants/proj/app.py", "/work/x.md", "/work/y.md"]


# ── read_file reads only the agent's roots, and folds the model's spelling first ────────────────


class _Reading(_Frame):
    def __init__(self, paths: Any = STANDARD_PATHS) -> None:
        super().__init__()
        self.paths = paths
        self.read: list[str] = []

    async def read_file(self, path: str) -> str:
        self.read.append(path)
        return "body"


def _read_tool(frame: Any, journal: Any = None) -> Any:
    return frame_toolset(("frame.read_file",), frame, journal or _Journal()).tools["read_file"].function


@pytest.mark.parametrize(
    "outside",
    [
        "/browser/profile/Cookies",
        "/work/../browser/profile/Cookies",
        "/verify/suite.py",
        "/run/secrets/x",
        "/etc/passwd",
        "../browser/profile/Cookies",
        "",
    ],
)
async def test_read_file_refuses_outside_the_roots(outside: str) -> None:
    frame, journal = _Reading(), _Journal()
    result = await _read_tool(frame, journal)(_Ctx(), outside)
    assert result.startswith("Refused:")
    assert "is outside the files you can read (/work, /artifacts, /task, /skills)" in result
    assert frame.read == []
    assert [c["ok"] for c in journal.calls] == [False], "a refusal is journalled, not raised"


def test_read_file_refuses_dotdot_escape() -> None:
    assert STANDARD_PATHS.agent_readable("/work/../browser/x") is None
    assert STANDARD_PATHS.agent_readable("/work/../task/context.md") == "/task/context.md"
    assert STANDARD_PATHS.agent_readable("/work/a\x00b") is None


async def test_read_file_allows_task_inputs() -> None:
    frame = _Reading()
    for path in ("/task/inputs/brief.md", "/skills/x/SKILL.md", "/artifacts/a.csv", "/work/a.txt"):
        assert await _read_tool(frame)(_Ctx(), path) == "     1\tbody"
    assert len(frame.read) == 4


async def test_read_file_normalises_dot_segments() -> None:
    frame = _Reading()
    for spelled in ("/work/./a.txt", "/work//a.txt", "/work/sub/../a.txt"):
        assert await _read_tool(frame)(_Ctx(), spelled) == "     1\tbody"
    assert frame.read == ["/work/a.txt"] * 3


async def test_write_file_normalises_dot_segments() -> None:
    frame = _Recording()
    write: Any = frame_toolset(("frame.write_file",), frame, _Journal()).tools["write_file"].function
    for spelled in ("/work/./a.txt", "/work//a.txt", "//work/a.txt"):
        assert (await write(_Ctx(), spelled, "x")).startswith("wrote ")
    assert frame.written == ["/work/a.txt"] * 3


async def test_a_device_frame_reads_its_grants_and_out() -> None:
    paths = replace(
        STANDARD_PATHS, on_host=True, out="/Users/me/out", grants=(("/grants/proj", "/Users/me/proj"),)
    )
    assert paths.agent_readable("/Users/me/proj/app.py") == "/grants/proj/app.py"
    assert paths.agent_readable("/grants/proj/app.py") == "/grants/proj/app.py"
    assert paths.agent_readable("/out/report.md") == "/out/report.md"
    assert paths.agent_readable("/Users/me/.ssh/id_rsa") is None
    assert STANDARD_PATHS.agent_readable("/grants/proj/app.py") is None


async def test_a_relative_name_written_is_read_back_under_work() -> None:
    class _Disk(_Reading):
        files: ClassVar[dict[str, str]] = {}

        async def write_file(self, path: str, content: str) -> None:
            self.files[path] = content

        async def read_file(self, path: str) -> str:
            self.read.append(path)
            return self.files[path]

    frame = _Disk()
    write: Any = frame_toolset(("frame.write_file",), frame, _Journal()).tools["write_file"].function
    await write(_Ctx(), "notes.md", "hello")
    assert await _read_tool(frame)(_Ctx(), "notes.md") == "     1\thello"
    assert frame.read == ["/work/notes.md"]
