# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`FrameSink` (ADR-063): a step's recording written into the frame
through its own file door, for a browser the worker drives. The recording's format is the
`Recorder`'s, unchanged; what this pins is where it goes and as whom - every file the system's
(`owner="system"`, 0644), so the agent reads its recording and never rewrites it."""

from __future__ import annotations

import json

from prismoid.ai.frames.frame_sink import FrameSink
from prismoid.ai.frames.image_browser.sidecar.recorder import Recorder, RecordingSink
from tests.support.fakes.frame_files import MemoryFiles

FOLDER = "/task/recordings/recording-s1-1"
CLICK = {"ok": True, "url": "https://plane.so/pricing"}
SHOT = {"target": "", "jpeg": b"\xff\xd8\xff"}


_FITS: RecordingSink = FrameSink(MemoryFiles())  # pyright checks the protocol


async def test_a_recording_lands_in_the_frame_as_the_systems_files() -> None:
    files = MemoryFiles()
    rec = Recorder(FrameSink(files), FOLDER)
    assert await rec.open()
    await rec.file("click", {"ref": "e1.2"}, CLICK, SHOT)
    done = await rec.finalize()
    assert done == {"ok": True, "actions": 1, "acting": 1, "path": FOLDER}
    assert files.files[f"{FOLDER}/0001-click.jpg"] == b"\xff\xd8\xff"
    assert json.loads(files.files[f"{FOLDER}/actions.json"])[0]["host"] == "plane.so"
    assert b"<h1>Recording" in files.files[f"{FOLDER}/index.html"]
    assert {(mode, owner) for _, mode, owner in files.writes} == {(0o644, "system")}


async def test_a_reopened_folder_continues_its_trail() -> None:
    """A second session of the same step (a reconnect, a retried call) picks the trail up, so a
    row never overwrites one already filed."""
    files = MemoryFiles()
    first = Recorder(FrameSink(files), FOLDER)
    await first.open()
    await first.file("click", {}, CLICK, SHOT)
    again = Recorder(FrameSink(files), FOLDER)
    await again.open()
    await again.file("click", {}, CLICK, SHOT)
    assert f"{FOLDER}/0002-click.jpg" in files.files
    assert [row["seq"] for row in json.loads(files.files[f"{FOLDER}/actions.json"])] == [1, 2]


async def test_a_folder_that_could_walk_anywhere_is_refused_and_records_nothing() -> None:
    files = MemoryFiles()
    for folder in ("/task/../etc", "recordings/r", "/", "/task//r"):
        assert not await FrameSink(files).open(folder), folder
    rec = Recorder(FrameSink(files), "/task/../etc")
    assert not await rec.open()
    await rec.file("click", {}, CLICK, SHOT)
    assert files.writes == []


async def test_a_name_that_is_not_a_plain_file_name_is_never_read_or_written() -> None:
    files = MemoryFiles()
    files.files["/task/secret"] = b"no"
    sink = FrameSink(files)
    assert await sink.open(f"{FOLDER}")
    assert await sink.read("../../secret") is None
    await sink.create("a/b.jpg", b"x")
    await sink.replace("..", "x")
    assert files.writes == []
    assert await sink.read("absent.json") is None
