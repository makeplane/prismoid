# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The real file door under the real `DeviceFrame` and the real file tools: what the helper answers
for a folder, and a publish staged in `out/` rather than the person's folder."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from prismoid.ai.frames.device_driver import DeviceFrame
from prismoid.ai.tools.frame_tools import frame_toolset
from prismoid.conf import settings
from prismoid.runner.device_door import create_app
from prismoid.schemas.device import FolderGrant
from prismoid.schemas.machines import LABEL_KIND, FrameKind


class _Call:
    tool_call_id = "toolu_1"


@pytest.fixture
async def frame(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[DeviceFrame]:
    monkeypatch.setattr(settings.local, "home", tmp_path / "home")
    (tmp_path / "proj" / "src").mkdir(parents=True)
    (tmp_path / "proj" / "README.md").write_text("theirs")
    app = create_app(tmp_path / "home", device_name=lambda: "Test Mac", chrome_path="/unused")
    made = DeviceFrame("t1", transport=httpx.ASGITransport(app=app))
    made.grant([FolderGrant(path=str(tmp_path / "proj"), mode="rw", name="/work")], False)
    await made.create(allow_domains=[], labels={LABEL_KIND: FrameKind.MACHINE.value})
    try:
        yield made
    finally:
        await app.state.egress.close()


async def test_a_folder_read_as_a_file_says_it_is_a_folder(frame: DeviceFrame) -> None:
    read: Any = frame_toolset(("frame.read_file",), frame).tools["read_file"].function
    answer = await read(_Call(), f"{frame.paths.work}/src")
    assert answer.startswith("Refused:") and "a folder" in answer


async def test_a_folder_written_as_a_file_says_it_is_a_folder(frame: DeviceFrame) -> None:
    write: Any = frame_toolset(("frame.write_file",), frame).tools["write_file"].function
    answer = await write(_Call(), f"{frame.paths.work}/src", "text")
    assert answer.startswith("Refused:") and "a folder" in answer


async def test_a_document_staged_in_out_publishes_and_leaves_their_file_alone(
    frame: DeviceFrame, tmp_path: Path
) -> None:
    assert frame.staging_dir is not None
    staged = f"{frame.staging_dir}/README.md"
    await frame.write_bytes(staged, b"# Ours\n")
    await frame.publish(staged, "document", "Ours", [])
    assert await frame.read_bytes("/artifacts/README.md") == b"# Ours\n"
    assert (tmp_path / "proj" / "README.md").read_text() == "theirs"
