# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every seam the agent reads speaks the frame's own paths (device Machine, A2): a cloud frame
keeps the virtual roots byte-for-byte, a frame on the user's disk gets its real ones."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from prismoid.ai.agents import all as _all  # the import IS the registration
from prismoid.ai.agents.base import IDENTITY, AgentName, instructions_for
from prismoid.ai.frames.driver import ExecResult
from prismoid.ai.frames.layout import STANDARD_PATHS, FramePaths
from prismoid.ai.tools.frame_tools import frame_toolset
from prismoid.ai.tools.providers.images import ImageCandidate, image_toolset
from prismoid.operations.execution import mounts
from prismoid.operations.execution.proof import check_proof
from prismoid.schemas.outputs import Outcome, Proof
from tests.support.fakes.fake_frames import FakeFrame

HOST = FramePaths(work="/W", task="/T", inputs="/T/inputs", on_host=True)


class _Ctx:
    tool_call_id = "toolu_seam_01"


def _today(defn: Any) -> str:
    """The join `instructions_for` made before paths existed."""
    return f"{IDENTITY}\n\n---\n\n{defn.prompt}" if defn.identity else defn.prompt


def _host_frame() -> FakeFrame:
    frame = FakeFrame("t1")
    frame.paths = HOST
    return frame


def test_every_agent_instructions_unchanged_on_virtual() -> None:
    assert _all.REGISTRY
    for defn in _all.REGISTRY.values():
        assert instructions_for(defn) == instructions_for(defn, STANDARD_PATHS) == _today(defn)


def test_instructions_render_on_host_paths() -> None:
    assert "/work" in instructions_for(_all.REGISTRY[AgentName("operator")])
    assert "/work" not in instructions_for(_all.REGISTRY[AgentName("operator")], HOST)


async def test_shell_docstring_and_missing_file_message_use_frame_paths() -> None:
    granted = ("frame.shell", "frame.read_file", "frame.write_file", "frame.publish")
    frame = _host_frame()

    async def missing(path: str) -> str:
        raise FileNotFoundError(path)

    frame.read_file = missing
    ts = frame_toolset(granted, frame, None, ("/task/inputs/a.pdf",))
    assert "/W" in (ts.tools["shell"].description or "")
    assert "/work" not in (ts.tools["shell"].description or "")
    read: Any = ts.tools["read_file"].function
    assert "/T/inputs/a.pdf" in await read(_Ctx(), "/W/missing.txt")


def test_cloud_tool_docstrings_are_untouched() -> None:
    granted = ("frame.shell", "frame.write_file", "frame.publish")
    ts = frame_toolset(granted, FakeFrame("t1"), None)
    assert "`/work`" in (ts.tools["write_file"].description or "")
    assert "python3 /work/build.py" in (ts.tools["shell"].description or "")


class _Journal:
    async def __call__(self, *a: Any, **k: Any) -> None: ...


class _Emit:
    async def __call__(self, units: dict[str, int]) -> None: ...


def _generate(monkeypatch: pytest.MonkeyPatch, frame: FakeFrame) -> Any:
    from prismoid.ai.tools.providers import images as media

    async def wire(cand: Any, prompt: str, size: str, n: int) -> list[bytes]:
        return [b"\x89PNG\r\n\x1a\n"]

    monkeypatch.setitem(media._WIRES, "openai_images", wire)  # pyright: ignore[reportPrivateUsage]

    class _NoThrottle:
        async def __aenter__(self) -> None: ...
        async def __aexit__(self, *a: Any) -> None: ...

    cand = ImageCandidate(
        provider="openai",
        model_id="m",
        protocol="openai_images",
        base_url=None,
        api_key="k",
        emit_units=_Emit(),
    )
    ts = image_toolset(
        ("media.generate_image",), frame, _Journal(), [cand], throttled=lambda _p: _NoThrottle()
    )
    assert ts is not None
    return ts.tools["generate_image"]


async def test_image_tool_accepts_real_work_path_on_host(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = _host_frame()
    tool = _generate(monkeypatch, frame)
    assert "/W" in (tool.description or "")
    result = await tool.function(_Ctx(), "a dawn", "/W/images/fig.png")
    assert result.startswith("generated 1 image") and "/W/images/fig.png" in result
    assert "/work/images/fig.png" in frame.files  # the frame's verbs take the virtual path
    refused = await tool.function(_Ctx(), "a dawn", "/etc/fig.png")
    assert refused.startswith("refused: path must live under /W/ and end in .png")


async def test_image_tool_refuses_outside_work_on_virtual(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _generate(monkeypatch, FakeFrame("t1"))
    refused = await tool.function(_Ctx(), "a dawn", "/etc/fig.png")
    assert refused == "refused: path must live under /work/ and end in .png - e.g. /work/images/figure.png"


@pytest.mark.parametrize("error", [ValueError("no"), OSError("no"), ConnectionError("no")])
async def test_a_cloud_frames_read_errors_stay_as_they_were(error: Exception) -> None:
    frame = FakeFrame("t1")

    async def refuses(path: str) -> str:
        raise error

    frame.read_file = refuses
    read: Any = frame_toolset(("frame.read_file",), frame, None, ()).tools["read_file"].function
    with pytest.raises(type(error)):
        await read(_Ctx(), "/work/src")


async def test_skill_md_rendered_scripts_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    from prismoid.ai.skills import format as skill_format
    from prismoid.operations import skills as skill_ops

    script = b"print('/work/out')\n"
    resolved = SimpleNamespace(
        name="demo",
        manifest=None,
        body="read /work/in.csv",
        files=[SimpleNamespace(path="scripts/run.py", data=script)],
    )

    async def resolve_pin(_session: Any, _pin: Any) -> Any:
        return resolved

    monkeypatch.setattr(skill_ops, "resolve_pin", resolve_pin)
    monkeypatch.setattr(skill_format, "render", lambda _m, body: f"# demo\n{body}")
    frame = _host_frame()
    spec = SimpleNamespace(skill_pins=[SimpleNamespace(ref=lambda: "p")])
    await mounts.materialise_skills(None, spec, frame)  # pyright: ignore[reportArgumentType]
    assert frame.files["/skills/demo/SKILL.md"][0] == b"# demo\nread /W/in.csv"
    assert frame.files["/skills/demo/scripts/run.py"][0] == script


def _pinned(monkeypatch: pytest.MonkeyPatch, files: list[SimpleNamespace]) -> SimpleNamespace:
    from prismoid.ai.skills import format as skill_format
    from prismoid.operations import skills as skill_ops

    resolved = SimpleNamespace(name="demo", manifest=None, body="", files=files)

    async def resolve_pin(_session: Any, _pin: Any) -> Any:
        return resolved

    monkeypatch.setattr(skill_ops, "resolve_pin", resolve_pin)
    monkeypatch.setattr(skill_format, "render", lambda _m, body: body)
    return SimpleNamespace(skill_pins=[SimpleNamespace(ref=lambda: "p")])


async def _materialised(spec: SimpleNamespace, frame: FakeFrame) -> None:
    materialise: Any = mounts.materialise_skills
    await materialise(None, spec, frame)


async def test_a_bundled_reference_speaks_the_device_frames_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recipe a model pastes into a command has to run there. Only text the model reads: a data
    file is the bytes the skill shipped, and a file that is not text is left alone."""
    data = b"/work/in.csv\n"
    binary = b"\xff/work\xfe"
    spec = _pinned(
        monkeypatch,
        [
            SimpleNamespace(path="references/recipe.md", data=b"cd /work && ls /task/inputs\n"),
            SimpleNamespace(path="references/SHOUT.MD", data=b"open /work/a\n"),
            SimpleNamespace(path="references/broken.md", data=binary),
            SimpleNamespace(path="assets/in.csv", data=data),
        ],
    )
    frame = _host_frame()
    await _materialised(spec, frame)
    assert frame.files["/skills/demo/references/recipe.md"][0] == b"cd /W && ls /T/inputs\n"
    assert frame.files["/skills/demo/references/SHOUT.MD"][0] == b"open /W/a\n"
    assert frame.files["/skills/demo/references/broken.md"][0] == binary
    assert frame.files["/skills/demo/assets/in.csv"][0] == data


async def test_a_bundled_reference_on_a_cloud_frame_lands_byte_for_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"cd /work && ls /task/inputs\n"
    spec = _pinned(monkeypatch, [SimpleNamespace(path="references/recipe.md", data=body)])
    frame = FakeFrame("t1")
    await _materialised(spec, frame)
    assert frame.files["/skills/demo/references/recipe.md"][0] == body


class _Lists:
    def __init__(self, paths: FramePaths, *names: str) -> None:
        self.paths, self._names = paths, names

    async def list_dir(self, path: str) -> list[str]:
        return list(self._names) if path == "/work" else []

    async def exec(self, command: str, timeout: float = 120.0) -> ExecResult:  # noqa: ASYNC109
        return ExecResult(1, "", "")  # no step marker: the proof keeps its existence check


async def test_proof_path_accepts_real_work_path_on_host() -> None:
    outcome = Outcome(status="done", summary="Saved.", proof=Proof(path="/W/out.csv"))
    assert await check_proof(outcome, frame=_Lists(HOST, "out.csv"), browser=None, published=()) is None
    virtual = _Lists(STANDARD_PATHS, "out.csv")
    assert await check_proof(outcome, frame=virtual, browser=None, published=()) is not None


def test_a_continued_run_is_told_in_the_frames_own_paths() -> None:
    from prismoid.ai.agents.run import Continue
    from prismoid.operations.execution.run import spoken

    told = Continue("attempt:2", "keep going in /work/out.csv")
    assert spoken(told, HOST) == Continue("attempt:2", "keep going in /W/out.csv")
    assert told.line == "keep going in /work/out.csv", "the receipt keeps the virtual roots"
    assert spoken("read /work/out.csv", HOST) == "read /W/out.csv"
