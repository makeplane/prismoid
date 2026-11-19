# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What the judge reads of a code artifact is bounded before it is read.

A `code` or `webapp` artifact is a tar the in-frame runner wrote, and the verify job opens it in the
shared worker to show the judge its source. Whoever ran the task chose its bytes, so the view reads
only a plain tar (publish never compresses; a compressed member stream is a bomb), only a slice of
each file, and only so many files.
"""

from __future__ import annotations

import io
import tarfile
import tracemalloc
from typing import IO, cast

from prismoid.operations.verification import CODE_VIEW_FILES, code_view


class Zeros:
    """`size` zero bytes, produced as they are read, so building the bomb holds none of them."""

    def __init__(self, size: int) -> None:
        self.left = size

    def read(self, n: int = -1) -> bytes:
        n = self.left if n < 0 else min(n, self.left)
        self.left -= n
        return b"\0" * n


def tar_of(files: dict[str, int], *, compressed: bool = False) -> bytes:
    """A tar of zero-filled files, `name -> size`, gzipped when asked."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") if compressed else tarfile.open(fileobj=out, mode="w") as tar:
        for name, size in files.items():
            info = tarfile.TarInfo(name)
            info.size = size
            tar.addfile(info, cast(IO[bytes], Zeros(size)))
    return out.getvalue()


def peak_of(payload: bytes) -> tuple[str, int]:
    tracemalloc.start()
    try:
        view = code_view(payload).text
        return view, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_a_project_reads_as_its_files() -> None:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        source = b"print('hello')\n"
        info = tarfile.TarInfo("app/main.py")
        info.size = len(source)
        tar.addfile(info, io.BytesIO(source))
    view = code_view(out.getvalue()).text
    assert "### app/main.py" in view and "print('hello')" in view


def test_a_compressed_archive_is_not_inflated() -> None:
    bomb = tar_of({"zeros.txt": 200_000_000}, compressed=True)  # ~200 KB on disk
    view, peak = peak_of(bomb)
    assert "could not be read" in view
    assert peak < 20_000_000, f"inflated {peak:,} bytes"


def test_a_huge_file_is_read_only_as_far_as_the_judge_reads() -> None:
    view, peak = peak_of(tar_of({"big.txt": 30_000_000}))
    assert "### big.txt" in view
    assert peak < 5_000_000, f"held {peak:,} bytes for an 8,000-character slice"


def test_a_crowd_of_files_is_read_only_to_the_cap() -> None:
    view = code_view(tar_of({f"f{n}.txt": 1 for n in range(CODE_VIEW_FILES * 3)})).text
    assert view.count("### ") <= CODE_VIEW_FILES
