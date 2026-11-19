# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Docker frame's `read_file` and `list_dir` hand a model-chosen path to the container as one
argv word: no shell ever parses it, so a quote, `$(...)`, a backtick, a space or a newline in a
name is only a name. A fake container runs each exec's argv on this machine in a temp directory.
"""

from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from prismoid.ai.frames.docker_driver import DockerFrame

HOSTILE = (
    "x'$(touch pwned)",
    'x"$(touch pwned)"',
    "x`touch pwned`",
    "x'`touch pwned`'",
    "a name with spaces",
    "two\nlines",
    "-n",
)


class _LocalContainer:
    """Runs an exec's argv here, in `cwd`, the way the daemon would run it in the frame."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd

    def exec_run(self, argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        done = subprocess.run(argv, cwd=self.cwd, capture_output=True, check=False)
        return SimpleNamespace(exit_code=done.returncode, output=(done.stdout or None, done.stderr or None))


class _LocalFrame(DockerFrame):
    """A Docker frame whose container is `_LocalContainer`."""

    def __init__(self, cwd: Path) -> None:
        super().__init__(task_id="t-paths")
        self._container = _LocalContainer(cwd)


@pytest.fixture
def frame(tmp_path: Path) -> DockerFrame:
    return _LocalFrame(tmp_path)


@pytest.mark.parametrize("name", HOSTILE)
async def test_a_hostile_file_name_is_read_and_runs_nothing(
    frame: DockerFrame, tmp_path: Path, name: str
) -> None:
    (tmp_path / name).write_text(f"body of {name}")

    assert await frame.read_file(str(tmp_path / name)) == f"body of {name}"
    assert not (tmp_path / "pwned").exists(), "a file name ran as a command"


@pytest.mark.parametrize("name", HOSTILE)
async def test_a_hostile_directory_name_is_listed_and_runs_nothing(
    frame: DockerFrame, tmp_path: Path, name: str
) -> None:
    folder = tmp_path / name
    folder.mkdir()
    (folder / "b.md").write_text("b")
    (folder / "a.md").write_text("a")

    assert await frame.list_dir(str(folder)) == ["a.md", "b.md"]
    assert not (tmp_path / "pwned").exists(), "a directory name ran as a command"


async def test_a_missing_file_is_file_not_found_naming_the_reason(frame: DockerFrame, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="No such file"):
        await frame.read_file(str(tmp_path / "x'$(touch pwned)"))
    assert not (tmp_path / "pwned").exists()


async def test_a_missing_directory_lists_as_nothing(frame: DockerFrame, tmp_path: Path) -> None:
    assert await frame.list_dir(str(tmp_path / "x'$(touch pwned)")) == []
    assert not (tmp_path / "pwned").exists()


class _ArchiveContainer:
    """Answers `get_archive` the way the daemon does: the path's own member first, then its contents."""

    def __init__(self, members: list[tuple[str, bytes | None]]) -> None:
        self.members = members

    def get_archive(self, _path: str) -> tuple[list[bytes], dict[str, Any]]:
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as tar:
            for name, data in self.members:
                info = tarfile.TarInfo(name)
                if data is None:
                    info.type = tarfile.DIRTYPE
                    tar.addfile(info)
                else:
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
        return [stream.getvalue()], {}


class _ArchiveFrame(DockerFrame):
    """A Docker frame whose container is `_ArchiveContainer`."""

    def __init__(self, members: list[tuple[str, bytes | None]]) -> None:
        super().__init__(task_id="t-archive")
        self._container = _ArchiveContainer(members)


async def test_read_bytes_returns_the_file_it_was_asked_for() -> None:
    assert await _ArchiveFrame([("memo.md", b"memo")]).read_bytes("/work/memo.md") == b"memo"


async def test_read_bytes_of_a_directory_is_not_a_file_inside_it() -> None:
    frame = _ArchiveFrame([("out", None), ("out/a.md", b"a file inside")])
    with pytest.raises(FileNotFoundError, match="directory"):
        await frame.read_bytes("/work/out")
