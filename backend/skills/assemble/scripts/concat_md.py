# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Mounted `NN-*.md` sections → ONE .md, disk to disk (ADR-025) - the markdown target.

    python3 concat_md.py --sections /task/inputs --out /work/report.md \
        --title "The Report" [--toc] [--subtitle "..."]

Title, generated contents, chapters concatenated in NN order with separators; a gap in the
sequence becomes a visible gap note. Figure markers pass through verbatim (markdown readers
render `![...]()` lines; caption markers stay honest text).
"""

from __future__ import annotations

import argparse

from md_walk import gap_note, section_files


def build(
    sections_dir: str, out: str, title: str, subtitle: str, toc: bool, total: int | None = None
) -> None:
    import os

    numbered: dict[int, tuple[str, str]] = {}
    for number, path in section_files(sections_dir).items():
        with open(path, encoding="utf-8") as handle:
            text = handle.read().strip()
        heading = next((ln.lstrip("#").strip() for ln in text.splitlines() if ln.startswith("#")), "")
        numbered[number] = (heading or os.path.basename(path)[3:-3], text)

    if not numbered:
        raise SystemExit(f"no NN-*.md sections found in {sections_dir}")

    # Count gaps up to the outline's expected total when given (--total), so a missing TRAILING
    # section is a visible gap note, not a silent drop off the end (finding 9). The caller
    # (operations, driving the maker) should pass section_total.
    end = max([*numbered, total or 0])
    parts = [f"# {title}", ""]
    if subtitle:
        parts += [f"_{subtitle}_", ""]
    if toc:
        parts += ["## Contents", ""]
        parts += [
            f"{n}. {numbered[n][0]}" if n in numbered else f"{n}. {gap_note(n)}" for n in range(1, end + 1)
        ]
        parts.append("")

    gaps = 0
    for number in range(1, end + 1):
        parts.append("\n---\n")
        if number not in numbered:
            parts.append(gap_note(number))
            gaps += 1
            continue
        heading, text = numbered[number]
        parts.append(text)
        print(f"  {number:02d} {heading}: {len(text.split())} words")

    body = "\n".join(parts).rstrip() + "\n"
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(body)
    print(f"saved {out}: {len(numbered)} sections, {len(body.split())} words, {gaps} gap note(s)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sections", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--toc", action="store_true")
    ap.add_argument(
        "--total", type=int, default=None, help="expected section count (outline's section_total)"
    )
    args = ap.parse_args()
    build(args.sections, args.out, args.title, args.subtitle, args.toc, args.total)
