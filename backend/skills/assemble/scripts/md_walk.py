# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The shared markdown walk for the assembly walkers (ADR-025). Stdlib only.

Pragmatic, not a CommonMark engine: the grammar is exactly what the section writers are taught
to emit - ATX headings, dash bullets, pipe tables, `![caption](path)` images, and the lane's
`Figure (fig-…): caption` markers. Anything else is a paragraph, verbatim.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field

FIGURE_MARKER = re.compile(r"^Figure \((?P<fig>fig-[\w-]+)\):\s*(?P<caption>.*)$")
IMAGE_LINE = re.compile(r"^!\[(?P<caption>[^\]]*)\]\((?P<path>[^)]+)\)\s*$")
SECTION_FILE = re.compile(r"^(?P<num>\d{2})-.+\.md$")

# Inline markdown the section writers emit reaches docx/pdf as literal glyphs (`**`, `` ` ``,
# `[x](y)`) - the QA rubric condemns those as painted formatting. Flatten them to plain text
# here, minimally and safely: links keep their visible text; **bold**/~~strike~~/`code` shed
# their markers. Single `*`/`_` italics are deliberately LEFT (stripping `_` would maul
# snake_case and `__dunder__` identifiers in technical documents - a worse defect than an
# italic glyph); the no-space guards keep `2 ** 3` and the like intact. Residual: __underscore
# bold__ is not stripped, for the same identifier-safety reason.
_INLINE_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_INLINE_BOLD = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*")
_INLINE_STRIKE = re.compile(r"~~(?!\s)(.+?)(?<!\s)~~")
_INLINE_CODE = re.compile(r"`([^`]+)`")


def _inline(text: str) -> str:
    text = _INLINE_LINK.sub(r"\1", text)
    text = _INLINE_BOLD.sub(r"\1", text)
    text = _INLINE_STRIKE.sub(r"\1", text)
    text = _INLINE_CODE.sub(r"\1", text)
    return text


@dataclass
class Block:
    kind: str  # heading | bullet | table | figure | para
    text: str = ""
    level: int = 0
    items: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    fig_id: str = ""


@dataclass
class Section:
    number: int
    title: str
    blocks: list[Block]


def parse(text: str) -> list[Block]:
    blocks: list[Block] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            blocks.append(Block("heading", text=_inline(line.lstrip("#").strip()), level=min(level, 4)))
            i += 1
            continue
        if figure := FIGURE_MARKER.match(line.strip()):
            blocks.append(Block("figure", text=_inline(figure["caption"]), fig_id=figure["fig"]))
            i += 1
            continue
        if image := IMAGE_LINE.match(line.strip()):
            stem = os.path.splitext(os.path.basename(image["path"]))[0]
            blocks.append(Block("figure", text=_inline(image["caption"]), fig_id=stem))
            i += 1
            continue
        if line.lstrip().startswith(("- ", "* ")):
            items = []
            while i < len(lines) and lines[i].lstrip().startswith(("- ", "* ")):
                items.append(_inline(lines[i].lstrip()[2:].strip()))
                i += 1
            blocks.append(Block("bullet", items=items))
            continue
        if line.lstrip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(set(c) <= {"-", ":", " "} for c in cells):  # skip the |---| rule
                    rows.append([_inline(c) for c in cells])
                i += 1
            if rows:
                blocks.append(Block("table", rows=rows))
            continue
        para = [line.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not _starts_block(lines[i]):
            para.append(lines[i].strip())
            i += 1
        blocks.append(Block("para", text=_inline(" ".join(para))))
    return blocks


def _starts_block(line: str) -> bool:
    s = line.lstrip()
    return s.startswith(("#", "- ", "* ", "|")) or bool(FIGURE_MARKER.match(s)) or bool(IMAGE_LINE.match(s))


def section_files(directory: str) -> dict[int, str]:
    """The mounted `NN-*.md` file chosen for each section number - every walker's one choice.

    When a number has BOTH a raw section and its harmonized `NN-…-final.md` (the seam pass,
    ADR-028), the -final file wins and the raw one is its fallback - decided HERE, in code,
    because a preference left to a prompt is a preference sometimes. (`-final.md` sorts BEFORE
    `.md`, so last-wins would pick the wrong file.)

    An EMPTY file is not a section: it is a mount an earlier step left in a Machine whose
    provider cannot unlink the system's files (sandbar empties them instead, ADR-063)."""
    chosen: dict[int, str] = {}
    for path in sorted(glob.glob(os.path.join(directory, "[0-9][0-9]-*.md"))):
        match = SECTION_FILE.match(os.path.basename(path))
        if not match or os.path.getsize(path) == 0:
            continue
        number = int(match["num"])
        already_final = chosen.get(number, "").endswith("-final.md")
        if number not in chosen or (path.endswith("-final.md") and not already_final):
            chosen[number] = path
    return chosen


def load_sections(directory: str, total: int | None = None) -> tuple[list[Section], list[int]]:
    """The mounted `NN-*.md` chapters in document order (`section_files`), plus the numbers
    MISSING from the sequence - the walkers turn those into visible gap notes, never a silent skip.

    `total` is the outline's expected section count (assembly's `section_total`): gaps are
    computed up to it, so a missing TRAILING section (sections 1–8 present of 10) is caught,
    not silently dropped off the end. Absent, gaps run only to the highest number seen - the
    pre-fix behavior. The caller (operations, driving the maker) should pass section_total."""
    chosen = section_files(directory)
    sections: list[Section] = []
    for number in sorted(chosen):
        path = chosen[number]
        name = os.path.basename(path)
        with open(path, encoding="utf-8") as handle:
            blocks = parse(handle.read())
        title = next((b.text for b in blocks if b.kind == "heading"), name[3:-3].replace("-", " "))
        sections.append(Section(number, title, blocks))

    numbers = [s.number for s in sections]
    expected = max([*numbers, total or 0], default=0)
    gaps = [n for n in range(1, expected + 1) if n not in numbers]
    return sections, gaps


def figure_path(images_dir: str | None, fig_id: str) -> str | None:
    """A generated figure's file, or None - and an empty file is none (a figure an earlier step
    mounted, emptied in place where its mount could not be removed)."""
    if not images_dir:
        return None
    for ext in (".png", ".jpg", ".webp"):
        candidate = os.path.join(images_dir, fig_id + ext)
        if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
            return candidate
    return None


def gap_note(number: int) -> str:
    return f"[Section {number} could not be produced]"
