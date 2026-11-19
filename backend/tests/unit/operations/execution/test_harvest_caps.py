# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Harvest reads what a step published under caps: so many entries, so many bytes together.

The manifest is a file in the frame, and the shared worker reads every entry it names into memory
before storing them. Whoever ran the task wrote both, so harvest refuses a manifest past
`HARVEST_MAX_ENTRIES`, stops past `HARVEST_MAX_BYTES` read, and names a file past one read's cap,
each as a failed attempt whose message is the fix. No database, no sandbox: a dict plays the frame.
"""

from __future__ import annotations

import datetime as dt

import pytest

from prismoid.ai.frames.driver import FileTooLarge
from prismoid.errors import PolicyDenied
from prismoid.operations.execution import harvest
from prismoid.operations.execution.harvest import HARVEST_MAX_ENTRIES, read_published
from prismoid.schemas.artifacts import Manifest, ManifestEntry
from prismoid.schemas.outputs import ArtifactType


class Frame:
    """The published files, and a log of every read the harvest made."""

    def __init__(self, files: dict[str, bytes], too_large: frozenset[str] = frozenset()) -> None:
        self.files, self.too_large, self.read = files, too_large, []

    async def read_bytes(self, path: str) -> bytes:
        self.read.append(path)
        if path in self.too_large:
            raise FileTooLarge(path, 1024)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


def manifest_of(*paths: str) -> Manifest:
    at = dt.datetime(2026, 11, 12, tzinfo=dt.UTC)
    entries = [
        ManifestEntry(
            path=p,
            type=ArtifactType.DOCUMENT,
            title=p,
            sha256="0" * 64,
            bytes=1,
            agent="writer",
            model="m",
            at=at,
        )
        for p in paths
    ]
    return Manifest(entries=entries)


async def test_every_published_file_is_read() -> None:
    frame = Frame({"/artifacts/a.md": b"a", "/artifacts/b.md": b"b"})
    payloads = await read_published(frame, manifest_of("/artifacts/a.md", "/artifacts/b.md"))
    assert payloads == {"/artifacts/a.md": b"a", "/artifacts/b.md": b"b"}


async def test_a_manifest_past_the_entry_cap_is_refused_before_any_read() -> None:
    paths = [f"/artifacts/{n}.md" for n in range(HARVEST_MAX_ENTRIES + 1)]
    frame = Frame(dict.fromkeys(paths, b"x"))
    with pytest.raises(PolicyDenied, match=f"more than the {HARVEST_MAX_ENTRIES}"):
        await read_published(frame, manifest_of(*paths))
    assert frame.read == []


async def test_reading_stops_once_the_step_passes_the_byte_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harvest, "HARVEST_MAX_BYTES", 2_500)
    paths = [f"/artifacts/{n}.bin" for n in range(5)]
    frame = Frame(dict.fromkeys(paths, b"x" * 1_000))
    with pytest.raises(PolicyDenied, match="together"):
        await read_published(frame, manifest_of(*paths))
    assert len(frame.read) == 3, "the read that passed the cap is the last one"


async def test_a_file_past_one_reads_cap_is_named_as_the_fix() -> None:
    frame = Frame({"/artifacts/a.md": b"a"}, too_large=frozenset({"/artifacts/huge.bin"}))
    with pytest.raises(PolicyDenied, match=r"huge\.bin"):
        await read_published(frame, manifest_of("/artifacts/a.md", "/artifacts/huge.bin"))
