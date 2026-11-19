# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Lead reads this turn's attachments before it runs: text as stored, a Word file through the
resource-limited conversion sidecar (the one real subprocess here), and an image never; what it
could not read is counted, so the turn plans a step for it."""

from __future__ import annotations

import pytest

from prismoid import storage
from prismoid.operations.execution import inline
from prismoid.schemas.inputs import TaskInput
from tests.support.documents import minimal_docx

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
CONTRACT = ("Clause 4: either party may end this agreement on 30 days notice. " * 100).encode()


def _input(name: str, media_type: str, data: bytes) -> TaskInput:
    return TaskInput(filename=name, sha256=name.ljust(64, "0"), bytes=len(data), media_type=media_type)


async def test_a_long_text_and_a_word_file_are_read_for_the_lead(monkeypatch: pytest.MonkeyPatch) -> None:
    blobs = {
        "contract.txt": CONTRACT,
        "memo.docx": minimal_docx("The board meets on Thursday."),
        "photo.png": b"\x89PNG\r\n",
        "broken.txt": b"\xff\xfe\xfa not utf-8",
    }
    items = [
        _input("contract.txt", "text/plain", blobs["contract.txt"]),
        _input("memo.docx", DOCX, blobs["memo.docx"]),
        _input("photo.png", "image/png", blobs["photo.png"]),
        _input("broken.txt", "text/plain", blobs["broken.txt"]),
    ]
    monkeypatch.setattr(storage, "get", lambda digest, bucket=None: blobs[digest.rstrip("0")])

    read = await inline.lead_files(items)

    assert len(CONTRACT) > 4096, "past the old 4 KB line"
    assert [f.path for f in read.files] == ["/task/inputs/contract.txt", "/task/inputs/memo.docx"]
    assert "30 days notice" in read.files[0].text and "board meets on Thursday" in read.files[1].text
    assert read.unread == 2, "the image and the undecodable file are a step's to read"
