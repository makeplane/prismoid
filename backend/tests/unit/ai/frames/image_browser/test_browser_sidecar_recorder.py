# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The recording's format, apart from any filesystem: a `Recorder` over
an in-memory sink, as a worker's sink writing into a remote frame is (ADR-063). The daemon's own sink
(`DirSink`, the fd-held folder) keeps its symlink and ownership tests in test_browser_sidecar_daemon.py."""

from __future__ import annotations

import base64
import json
import sys

from tests.support.paths import BACKEND

SIDECAR = BACKEND / "prismoid" / "ai" / "frames" / "image_browser"
sys.path.insert(0, str(SIDECAR))

from sidecar.recorder import Recorder  # noqa: E402


class MemorySink:
    """Folders of named bytes; `create` refuses a name twice, as the daemon's O_EXCL does."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.folders: dict[str, dict[str, bytes]] = {}
        self.sealed: list[str] = []
        self.folder = ""
        self._ready = False
        self._refuse = refuse

    @property
    def ready(self) -> bool:
        return self._ready

    async def open(self, folder: str) -> bool:
        self.folder, self._ready = folder, not self._refuse
        if self._ready:
            self.folders.setdefault(folder, {})
        return self._ready

    async def read(self, name: str) -> bytes | None:
        return self.folders[self.folder].get(name)

    async def create(self, name: str, data: bytes) -> None:
        assert name not in self.folders[self.folder], "a recording never overwrites a picture"
        self.folders[self.folder][name] = data

    async def replace(self, name: str, text: str) -> None:
        self.folders[self.folder][name] = text.encode()

    async def seal(self) -> None:
        self.sealed.append(self.folder)


async def test_an_act_files_its_row_and_picture_named_from_the_last_snapshot() -> None:
    sink = MemorySink()
    rec = Recorder(sink, "/artifacts/recording-1")
    assert await rec.open()
    # The look's own (role, name) per reference: a name whose words mimic another line is just a name.
    rec.remember_targets(
        {"e1.1": ("button", "Go"), "e1.2": ("link", '[e1.1] link "Delete"'), "e1.3": ("main", "")}
    )
    await rec.file(
        "click",
        {"ref": "e1.1"},
        {"ok": True, "url": "https://a.example/b?q=1"},
        {"target": "", "jpeg": b"\xff\xd8"},
    )
    files = sink.folders["/artifacts/recording-1"]
    assert files["0001-click.jpg"] == b"\xff\xd8"
    (row,) = json.loads(files["actions.json"])
    assert (row["verb"], row["target"], row["host"], row["path"], row["file"]) == (
        "click",
        'button "Go"',
        "a.example",
        "/b",
        "0001-click.jpg",
    )
    await rec.file("click", {"ref": "e1.2"}, {"ok": True, "url": "https://a.example/"}, {"target": ""})
    await rec.file("click", {"ref": "e1.3"}, {"ok": True, "url": "https://a.example/"}, {"target": ""})
    rows = json.loads(files["actions.json"])
    assert [r["target"] for r in rows[1:]] == ['link "[e1.1] link "Delete""', "main"]


async def test_a_screenshot_is_filed_at_full_size_and_a_failed_capture_still_counts() -> None:
    sink = MemorySink()
    rec = Recorder(sink, "/artifacts/recording-1")
    await rec.open()
    png = base64.b64encode(b"\x89PNG").decode()
    await rec.file("screenshot", {}, {"ok": True, "png_b64": png, "url": "https://a.example/"}, None)
    await rec.file("type", {"ref": "e9.9"}, {"ok": True, "url": "https://a.example/"}, {"capture": "failed"})
    rows = json.loads(sink.folders["/artifacts/recording-1"]["actions.json"])
    assert [(r["verb"], r["file"], r.get("capture")) for r in rows] == [
        ("screenshot", "0001-screenshot.png", None),
        ("type", "", "failed"),
    ]


async def test_a_folder_picks_up_its_own_trail_and_finalize_indexes_and_seals_it() -> None:
    sink = MemorySink()
    rec = Recorder(sink, "/artifacts/recording-2")
    await rec.open()
    await rec.row("open", "", b"\xff\xd8", "jpg", "https://a.example/")
    again = Recorder(sink, "/artifacts/recording-2")  # a daemon restarted under the same step
    await again.open()
    await again.row("click", 'button "Go"', b"\xff\xd8", "jpg", "https://a.example/")
    done = await again.finalize()
    assert done == {"ok": True, "actions": 2, "acting": 2, "path": "/artifacts/recording-2"}
    files = sink.folders["/artifacts/recording-2"]
    assert "0002-click.jpg" in files and "data:image/jpeg;base64," in files["index.html"].decode()
    assert sink.sealed == ["/artifacts/recording-2"]


async def test_a_refused_folder_records_nothing_and_finalize_says_so() -> None:
    rec = Recorder(MemorySink(refuse=True), "/artifacts/recording-1")
    assert await rec.open() is False and not rec.recording
    await rec.row("click", "", b"\xff\xd8", "jpg", "https://a.example/")
    assert await rec.finalize() == {"ok": False, "error": "recording folder unavailable"}
