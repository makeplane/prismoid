# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""On the device a rendition needs LibreOffice, and without it the lane is off: the preview runner
is told so, and a task never fails for it. Cloud's arguments and command line stay as they were."""

from __future__ import annotations

from typing import Any

import orjson
import pytest

from prismoid.ai.frames.driver import ExecResult, run_python
from prismoid.ai.frames.layout import FramePaths
from prismoid.ai.frames.preview import ARGS_PATH, OUT_PATH, RUNNER_PATH, preview_args
from prismoid.conf import settings
from prismoid.operations.execution import harvest as ex_harvest
from prismoid.schemas.artifacts import ManifestEntry
from prismoid.schemas.outputs import ArtifactType
from tests.support.fakes.fake_frames import FakeFrame


def _on_host() -> FramePaths:
    return FramePaths(
        task="/p/task",
        work="/p/work",
        artifacts="/p/artifacts",
        verify="/p/verify",
        out="/p/out",
        on_host=True,
    )


def test_cloud_preview_args_are_unchanged() -> None:
    entries = [{"path": "/artifacts/a.docx", "type": "document"}]
    assert preview_args(entries, "n") == {"entries": entries, "out": OUT_PATH, "nonce": "n"}


class _DeviceFake(FakeFrame):
    """A device's system door over the fake: what the engine sent, and a canned runner result."""

    def __init__(self) -> None:
        super().__init__("t")
        self.paths = _on_host()  # pyright: ignore[reportIncompatibleVariableOverride]
        self.ran: list[tuple[str, tuple[str, ...]]] = []
        self.outcome = ExecResult(0, "", "")

    async def system_python(
        self,
        script: str,
        *args: str,
        stdin: str | None = None,
        timeout: float = 120.0,  # noqa: ASYNC109
    ) -> ExecResult:
        self.ran.append((script, args))
        return self.outcome

    async def exec(self, command: str, timeout: float = 120.0) -> ExecResult:  # noqa: ASYNC109
        raise AssertionError(f"an agent exec of the system's Python: {command}")

    async def write_bytes(self, path: str, data: bytes, **kw: Any) -> None:
        self.written[path] = data

    async def read_bytes(self, path: str) -> bytes:
        return self.returned[path]

    written: dict[str, bytes]
    returned: dict[str, bytes]


def _frame() -> _DeviceFake:
    frame = _DeviceFake()
    frame.written, frame.returned = {}, {}
    return frame


class _Manifest:
    def __init__(self, *paths: str) -> None:
        self.entries = {p: ManifestEntry.model_construct(path=p, type=ArtifactType.DOCUMENT) for p in paths}

    def latest(self) -> dict[str, ManifestEntry]:
        return self.entries


async def test_preview_degrades_without_office(monkeypatch: pytest.MonkeyPatch) -> None:
    """No LibreOffice: the runner is told to skip the rendition, and the extracted preview stands."""
    monkeypatch.setattr(settings.local, "has_office", False)
    frame = _frame()
    args: dict[str, Any] = {}

    async def run(script: str, *rest: str, **kw: Any) -> ExecResult:
        args.update(orjson.loads(frame.written[ARGS_PATH]))
        path = args["entries"][0]["path"]
        made = {path: {"preview": {"kind": "document", "blocks": []}, "rendition": None}}
        frame.returned[args["out"]] = orjson.dumps({"nonce": args["nonce"], "made": made})
        return ExecResult(0, "", "")

    monkeypatch.setattr(frame, "system_python", run)
    previews, renditions = await ex_harvest.extract_previews(frame, _Manifest("/artifacts/a.docx"))
    assert args["office"] is False
    assert args["entries"] == [{"path": "/p/artifacts/a.docx", "type": "document"}]
    assert list(previews) == ["/artifacts/a.docx"], "the record keeps the virtual path"
    assert renditions == {}


async def test_with_office_the_runner_may_render(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.local, "has_office", True)
    frame = _frame()
    frame.outcome = ExecResult(127, "", "gone")
    await ex_harvest.extract_previews(frame, _Manifest("/artifacts/a.docx"))
    args = orjson.loads(frame.written[ARGS_PATH])
    assert args["office"] is True
    assert args["out"] == "/p/artifacts/.plane-previews.json"


async def test_a_runner_that_cannot_run_is_no_previews(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runtime is missing: a harvest carries on with nothing to show, never a failed task."""
    frame = _frame()
    frame.outcome = ExecResult(127, "", "Prismoid's runtime is missing")
    assert await ex_harvest.extract_previews(frame, _Manifest("/artifacts/a.docx")) == ({}, {})
    assert frame.ran == [(RUNNER_PATH, (ARGS_PATH,))]


async def test_a_cloud_frame_keeps_its_fixed_command_lines() -> None:
    commands: list[str] = []

    class Cloud(FakeFrame):
        async def exec(self, command: str, timeout: float = 120.0) -> ExecResult:  # noqa: ASYNC109
            commands.append(command)
            return ExecResult(0, "", "")

    frame = Cloud("t")
    await run_python(frame, RUNNER_PATH, ARGS_PATH, timeout=1.0)
    await run_python(frame, "/work/q.py", stdin="/work/j.json", timeout=1.0)
    await run_python(
        frame, "/verify/s.py", stdin="/verify/j.json", timeout=1.0, interpreter="/usr/local/bin/python3"
    )
    assert commands == [
        f"python3 {RUNNER_PATH} {ARGS_PATH}",
        "python3 /work/q.py < /work/j.json",
        "/usr/local/bin/python3 /verify/s.py < /verify/j.json",
    ]
