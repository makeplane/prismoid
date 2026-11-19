# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`edit` changes a file by small replacements and `read_file` reads one in numbered windows: a made edit
shows the lines it changed, a miss hands back what the next try needs (the nearest lines, numbered), a
match that is not unique is refused with its count, and a window reads on to the end. Driven through the
real toolset on a paper frame, where `edit` runs like `write_file` without a computer."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pytest

from prismoid.ai.capabilities.machine_acquire import needs_machine
from prismoid.ai.frames import FrameDriver, new_paper
from prismoid.ai.tools.frame_tools import frame_toolset
from prismoid.ai.tools.output import LINE_CAP, OUTPUT_CAP

GRANTED = ("frame.write_file", "frame.edit", "frame.read_file")
CODE = "def total(xs):\n    s = 0\n    for x in xs:\n        s += x\n    return s\n\n\ndef mean(xs):\n    return total(xs) / len(xs)\n"


class _Ctx:
    tool_call_id = "toolu_01"


@dataclass
class _Journal:
    rows: list[tuple[str, bool, str]] = field(default_factory=list[tuple[str, bool, str]])

    async def __call__(
        self,
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        *,
        ref: str | None = None,
    ) -> None:
        self.rows.append((tool, ok, denied_reason))


async def _frame(**files: str) -> FrameDriver:
    frame = new_paper("t-s-1", max_bytes=1 << 20)
    for name, text in files.items():
        await frame.write_bytes(f"/work/{name.replace('_', '.')}", text.encode())
    return frame


def _tools(frame: FrameDriver, journal: _Journal | None = None) -> Any:
    tools = frame_toolset(GRANTED, frame, journal).tools
    return tools["edit"].function, tools["read_file"].function


async def _text(frame: FrameDriver, path: str) -> str:
    return (await frame.read_bytes(path)).decode()


async def test_an_edit_replaces_one_match_and_shows_the_lines_it_changed() -> None:
    frame = await _frame(app_py=CODE)
    edit, _ = _tools(frame)

    said = await edit(
        _Ctx(), "/work/app.py", "    return total(xs) / len(xs)", "    return total(xs) / max(1, len(xs))"
    )

    assert await _text(frame, "/work/app.py") == CODE.replace("/ len(xs)", "/ max(1, len(xs))")
    assert said.startswith("edited /work/app.py"), said
    assert "     9\t    return total(xs) / max(1, len(xs))" in said, (
        "the changed line, numbered as read_file shows it"
    )


async def test_a_match_that_is_not_unique_is_refused_with_its_count_and_replace_all_takes_every_one() -> None:
    frame = await _frame(app_py=CODE)
    journal = _Journal()
    edit, _ = _tools(frame, journal)

    said = await edit(_Ctx(), "/work/app.py", "xs", "items")

    assert "matches 5 places (starting on lines 1, 3, 8, 9)" in said and "replace_all" in said, said
    assert await _text(frame, "/work/app.py") == CODE, "a refused edit writes nothing"
    assert journal.rows == [("frame.edit", False, "ambiguous")], "the row holds the reason, never the text"

    said = await edit(_Ctx(), "/work/app.py", "xs", "items", replace_all=True)
    assert said == "edited /work/app.py: replaced 5 places."
    assert "xs" not in await _text(frame, "/work/app.py")


async def test_a_miss_shows_the_nearest_lines_and_copying_them_makes_the_edit() -> None:
    """The model typed the loop at the wrong indentation: the hint shows the real lines, numbered, and
    the text after each tab is what the next `old_string` needs, with no re-read."""
    frame = await _frame(app_py=CODE)
    edit, _ = _tools(frame)

    missed = await edit(_Ctx(), "/work/app.py", "for x in xs:\n    s += x", "for x in xs:\n    s += 2 * x")

    assert "not in the file" in missed and await _text(frame, "/work/app.py") == CODE
    shown = re.findall(r"^ *(\d+)\t(.*)$", missed, re.MULTILINE)
    assert ("3", "    for x in xs:") in shown and ("4", "        s += x") in shown, missed
    old = "\n".join(text for n, text in shown if n in {"3", "4"})

    said = await edit(_Ctx(), "/work/app.py", old, "    for x in xs:\n        s += 2 * x")
    assert said.startswith("edited"), said
    assert "        s += 2 * x\n" in await _text(frame, "/work/app.py")


async def test_a_miss_with_no_line_alike_says_to_read_the_file() -> None:
    frame = await _frame(app_py=CODE)
    edit, _ = _tools(frame)

    said = await edit(_Ctx(), "/work/app.py", "class Ledger:", "class Book:")

    assert "no line resembles" in said and "read_file" in said


@pytest.mark.parametrize(
    ("held", "old", "new", "made"),
    [
        ("a = 1   \nb = 2\n", "a = 1\nb = 2", "a = 3\nb = 4", "a = 3\nb = 4\n"),  # trailing whitespace
        (
            "a = 1\r\nb = 2\r\n",
            "a = 1\nb = 2\n",
            "a = 3\nb = 4\n",
            "a = 3\r\nb = 4\r\n",
        ),  # the file's line endings
        ("say(“hi”) — ok\n", 'say("hi") - ok', 'say("bye")', 'say("bye")\n'),  # typographic marks, em-dash-ok
    ],
)
async def test_the_ladder_forgives_what_a_model_cannot_see(held: str, old: str, new: str, made: str) -> None:
    frame = await _frame(f_txt=held)
    edit, _ = _tools(frame)

    said = await edit(_Ctx(), "/work/f.txt", old, new)

    assert said.startswith("edited"), said
    assert await _text(frame, "/work/f.txt") == made


async def test_the_ladder_never_forgives_indentation() -> None:
    frame = await _frame(app_py=CODE)
    edit, _ = _tools(frame)

    said = await edit(_Ctx(), "/work/app.py", "s = 0", "s = 1")  # exact, inside its line: fine
    assert said.startswith("edited")
    said = await edit(_Ctx(), "/work/app.py", "return s\n", "return -s\n")
    assert said.startswith("edited")
    said = await edit(_Ctx(), "/work/app.py", "def mean(xs):\nreturn total(xs) / len(xs)", "x")
    assert "not in the file" in said


@pytest.mark.parametrize(
    ("old", "new", "said"),
    [
        ("s = 0", "s = 0", "nothing to change"),
        ("", "import os\n", "write_file"),
        ("s = -1", "    return s", "looks made already"),
    ],
)
async def test_an_edit_that_cannot_change_anything_says_why(old: str, new: str, said: str) -> None:
    frame = await _frame(app_py=CODE)
    edit, _ = _tools(frame)

    assert said in await edit(_Ctx(), "/work/app.py", old, new)
    assert await _text(frame, "/work/app.py") == CODE


async def test_a_missing_file_a_binary_one_and_a_path_outside_work_are_answers() -> None:
    frame = await _frame()
    await frame.write_bytes("/work/blob.bin", b"\xff\xfe\x00abc")
    edit, _ = _tools(frame)

    assert "does not exist. Create it with write_file" in await edit(_Ctx(), "/work/new.py", "a", "b")
    assert "not UTF-8 text" in await edit(_Ctx(), "/work/blob.bin", "abc", "xyz")
    assert await frame.read_bytes("/work/blob.bin") == b"\xff\xfe\x00abc", "never written back mangled"
    assert (await edit(_Ctx(), "/task/plan.md", "a", "b")).startswith("Refused:")


def test_an_edit_stays_on_paper_like_a_write() -> None:
    """Both change only files, through the frame's file API: neither starts the Machine."""
    assert not needs_machine("frame.edit") and not needs_machine("frame.write_file")
    assert needs_machine("frame.shell")


async def test_read_file_is_numbered_and_whole_when_it_fits() -> None:
    frame = await _frame(app_py=CODE)
    _, read = _tools(frame)

    said = await read(_Ctx(), "/work/app.py")

    assert said.split("\n")[:2] == ["     1\tdef total(xs):", "     2\t    s = 0"]
    assert "[lines" not in said, "a whole file needs no footer"


async def test_read_file_windows_paginate_to_the_end() -> None:
    lines = [f"row {n:05d} " + "x" * 40 for n in range(1, 2001)]
    frame = await _frame(big_txt="\n".join(lines) + "\n")
    _, read = _tools(frame)

    seen: list[str] = []
    offset, windows = 1, 0
    while True:
        said = await read(_Ctx(), "/work/big.txt", offset)
        windows += 1
        body, _, footer = said.rpartition("\n\n")
        assert len(body) <= OUTPUT_CAP
        rows = [row.split("\t", 1) for row in body.split("\n")]
        assert int(rows[0][0]) == offset, "a window starts where it was asked to"
        seen += [text for _, text in rows]
        if "the end of the file" in footer:
            break
        on = re.search(r"offset=(\d+)", footer)
        assert on is not None, footer
        offset = int(on.group(1))
    assert seen == lines and windows > 1


async def test_read_file_takes_a_limit_and_cuts_a_long_line() -> None:
    frame = await _frame(f_txt="a\n" + "y" * (LINE_CAP + 50) + "\nc\nd\n")
    _, read = _tools(frame)

    said = await read(_Ctx(), "/work/f.txt", 2, 2)

    assert said.startswith("     2\t" + "y" * LINE_CAP + " [... line cut]\n     3\tc")
    assert said.endswith("[lines 2 to 3 of 4: read on with offset=4]")
    assert "past its end" in await read(_Ctx(), "/work/f.txt", 9)
