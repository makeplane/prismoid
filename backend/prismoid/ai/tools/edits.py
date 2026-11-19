# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A str_replace edit, matched the way the leading harnesses match one, and a miss the model can act on.

`old_string` is found exactly first (Claude Code's Edit). Failing that, a short ladder over whole lines
(Codex's `seek_sequence`, opencode's edit): the file's own line endings, then trailing whitespace
ignored, then typographic quotes, dashes and spaces read as ASCII. It is never fuzzier than that, so
indentation always counts. A match must be unique unless every match is asked for. A miss says why
and shows the file's nearest lines, numbered as `read_file` numbers them, so the next try needs no
re-read. Pure: text in, text out.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Final, Literal

from prismoid.ai.tools.output import numbered

# Codex's `seek_sequence` normalisation: the punctuation a model types plainly and a file may hold fancy.
_FOLD: Final = str.maketrans(
    {
        **dict.fromkeys(map(chr, range(0x2018, 0x201C)), "'"),  # single quotes
        **dict.fromkeys(map(chr, range(0x201C, 0x2020)), '"'),  # double quotes
        **dict.fromkeys([*map(chr, range(0x2010, 0x2016)), chr(0x2212)], "-"),  # hyphens, dashes, minus
        **dict.fromkeys(
            [chr(0xA0), *map(chr, range(0x2002, 0x200B)), *map(chr, (0x202F, 0x205F, 0x3000))], " "
        ),
    }
)

# Each rung reads a line more loosely than the one before; none touches leading whitespace.
_RUNGS: Final[tuple[Callable[[str], str], ...]] = (
    lambda line: line.removesuffix("\r"),
    str.rstrip,
    lambda line: line.rstrip().translate(_FOLD),
)

# How alike a line must be to `old_string`'s first line to be shown as the nearest.
_NEAR: Final = 0.6
# The most match lines an ambiguous miss names, and the most lines a hint or a result shows.
_NAMED: Final = 10
_SHOWN: Final = 40

Reason = Literal["empty", "no_change", "not_found", "ambiguous", "already_applied"]


@dataclass(frozen=True)
class Edited:
    """The file after the edit, how many places changed, and the lines the first change now spans."""

    text: str
    replaced: int
    first_line: int
    lines: int


@dataclass(frozen=True)
class Missed:
    """Why the edit was not made, and what the model reads to make it next time."""

    reason: Reason
    said: str


def apply(text: str, old: str, new: str, *, replace_all: bool = False) -> Edited | Missed:
    """`text` with `old` replaced by `new` once (or everywhere, with `replace_all`), or why not."""
    if not old:
        return Missed(
            "empty",
            "old_string is empty. Create a new file with write_file; to add to a file, "
            "set old_string to the lines next to where the text goes and repeat them in new_string.",
        )
    if old == new:
        return Missed("no_change", "old_string and new_string are the same: there is nothing to change.")
    if text.count("\r\n") * 2 > text.count("\n"):
        new = new.replace("\r\n", "\n").replace("\n", "\r\n")
    spans = _exact(text, old)
    for rung in _RUNGS:
        spans = spans or _line_spans(text, old, rung)
    if not spans:
        return _miss(text, old, new)
    if len(spans) > 1 and not replace_all:
        starts = dict.fromkeys(_line_of(text, start) for start, _ in spans)
        named = ", ".join(str(n) for n in list(starts)[:_NAMED])
        return Missed(
            "ambiguous",
            f"old_string matches {len(spans)} places (starting on lines {named}). Add the lines around the "
            "one you mean to old_string so it matches once, or set replace_all to change every one.",
        )
    out = text
    for start, end in reversed(spans):
        out = out[:start] + new + out[end:]
    return Edited(out, len(spans), _line_of(text, spans[0][0]), new.count("\n") + 1)


def _exact(text: str, old: str) -> list[tuple[int, int]]:
    """Where `old` occurs in `text`, without overlaps."""
    spans: list[tuple[int, int]] = []
    at = text.find(old)
    while at >= 0:
        spans.append((at, at + len(old)))
        at = text.find(old, at + len(old))
    return spans


def _line_spans(text: str, old: str, rung: Callable[[str], str]) -> list[tuple[int, int]]:
    """Where `old`'s whole lines match `text`'s under `rung`, without overlaps. The last line ends before
    its line break, or after it when `old` ends with one."""
    lines = text.split("\n")
    seen = [rung(line) for line in lines]
    want = [rung(line) for line in old.removesuffix("\n").split("\n")]
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line) + 1)
    spans: list[tuple[int, int]] = []
    i, k, eol = 0, len(want), old.endswith("\n")
    while i + k + eol <= len(lines):
        if seen[i : i + k] == want:
            last = i + k - 1
            end = starts[last + 1] if eol else starts[last] + len(lines[last].removesuffix("\r"))
            spans.append((starts[i], end))
            i += k
        else:
            i += 1
    return spans


def _line_of(text: str, at: int) -> int:
    return text.count("\n", 0, at) + 1


def _miss(text: str, old: str, new: str) -> Missed:
    """`old` is nowhere in `text`: say whether the edit is in already, else show the nearest lines."""
    if new.strip() and (at := text.find(new)) >= 0:
        return Missed(
            "already_applied",
            f"old_string is not in the file, but new_string is (from line {_line_of(text, at)}): this edit "
            "looks made already. Read those lines before editing again.",
        )
    near = _nearest(text, old)
    if near is None:
        return Missed(
            "not_found",
            "old_string is not in the file and no line resembles its first line. Read the file with "
            "read_file and copy old_string from it exactly.",
        )
    span = min(_SHOWN, old.count("\n") + 3)
    return Missed(
        "not_found",
        "old_string is not in the file. The nearest lines, numbered as read_file numbers them, are below: "
        "copy old_string from the text after each tab, indentation included, without the numbers.\n\n"
        + numbered(text, max(1, near - 1), span),
    )


def _nearest(text: str, old: str) -> int | None:
    """The 1-based line of `text` most like `old`'s first non-blank line, when one is alike enough."""
    anchor = next((line.strip() for line in old.split("\n") if line.strip()), "")
    if not anchor:
        return None
    matcher = SequenceMatcher(autojunk=False)
    matcher.set_seq2(anchor)
    best, found = _NEAR, None
    for n, line in enumerate(text.split("\n"), start=1):
        matcher.set_seq1(line.strip())
        if matcher.real_quick_ratio() >= best and matcher.quick_ratio() >= best and matcher.ratio() > best:
            best, found = matcher.ratio(), n
    return found
