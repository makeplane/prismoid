# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The copyright header (ADR-102): every tracked source file opens with the notice in COPYRIGHT.txt.

`python tools/copyright_header.py` (from backend/; `just copyright`, part of `just check` and the
Copyright workflow) names every tracked source file without the notice and exits 1; `--fix` writes
it into each of them. The notice is COPYRIGHT.txt's lines in the file's own comment syntax, after a
shebang when there is one, then a blank line. Standard library only, so the workflow runs it with the
runner's own Python and installs nothing.

Not headed: vendored code (backend/vendor/ keeps its own notices), generated code (`*.gen.ts`, which
`pnpm gen` rewrites), type declarations (`*.d.ts`) and empty files. `--fix` never writes over or
above a copyright already near a file's top: someone else's notice is not ours to cover, so the file
is named for a person to decide.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NOTICE = "COPYRIGHT.txt"  # at the root of the tree being checked
HASH, SLASHES, BLOCK = "#", "//", "/**"
STYLES = {
    ".py": HASH,
    ".sh": HASH,
    ".go": SLASHES,
    ".ts": BLOCK,
    ".tsx": BLOCK,
    ".js": BLOCK,
    ".mjs": BLOCK,
    ".cjs": BLOCK,
    ".css": BLOCK,
}
SKIPPED_DIRS = ("backend/vendor/",)
SKIPPED_SUFFIXES = (".gen.ts", ".d.ts")
NEAR_TOP = 25
NOTICE_START = re.compile(r"""\s*(?:#|//|/\*+|\*|"{3}|'{3})?\s*copyright\b\s*(?:\(c\)|©|[0-9]{4})""", re.I)


def render(style: str, notice: list[str]) -> list[str]:
    """The notice in one comment syntax: `#` or `//` lines, or a `/** */` block."""
    if style == BLOCK:
        return ["/**", *(f" * {line}" for line in notice), " */"]
    return [f"{style} {line}" for line in notice]


def headed(rel: str) -> bool:
    """Whether a tracked path must carry the header."""
    return (
        Path(rel).suffix in STYLES and not rel.startswith(SKIPPED_DIRS) and not rel.endswith(SKIPPED_SUFFIXES)
    )


def _shebang(lines: list[str]) -> int:
    return 1 if lines and lines[0].startswith("#!") else 0


def has_header(text: str, header: list[str]) -> bool:
    """The header opens the file, after a shebang if there is one."""
    lines = text.splitlines()
    start = _shebang(lines)
    return lines[start : start + len(header)] == header


def with_header(text: str, header: list[str]) -> str:
    """The text with the header written in after its shebang, then one blank line before the rest."""
    lines = text.splitlines(keepends=True)
    start = _shebang(lines)
    head = "".join(lines[:start])
    if head and not head.endswith("\n"):
        head += "\n"
    rest = "".join(lines[start:]).lstrip("\r\n")
    return head + "\n".join(header) + "\n" + ("\n" + rest if rest else "")


def foreign(text: str) -> bool:
    """A copyright notice already near the top (a line that opens `Copyright (c)`, `©` or a year, in a
    comment or a docstring): someone else's notice, or ours mangled."""
    return any(NOTICE_START.match(line) for line in text.splitlines()[:NEAR_TOP])


def sources(root: Path) -> list[str]:
    """The tracked files under `root` that must carry the header."""
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True, text=True
    ).stdout
    return sorted(rel for rel in out.split("\0") if rel and headed(rel) and (root / rel).is_file())


def _read(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as f:  # newline="": the rest of the file keeps its bytes
        return f.read()


def _headers(root: Path) -> dict[str, list[str]]:
    """The header for each suffix, rendered from `root`'s COPYRIGHT.txt."""
    notice = (root / NOTICE).read_text(encoding="utf-8").splitlines()
    return {suffix: render(style, notice) for suffix, style in STYLES.items()}


def unheaded(root: Path = ROOT) -> list[str]:
    """Every tracked source file under `root` that is not empty and does not open with the header."""
    headers = _headers(root)
    return [
        rel
        for rel in sources(root)
        if (text := _read(root / rel)) and not has_header(text, headers[Path(rel).suffix])
    ]


def fix(root: Path = ROOT) -> list[str]:
    """Write the header into every unheaded file; answer the files left alone (a copyright near the top)."""
    headers, refused = _headers(root), list[str]()
    for rel in unheaded(root):
        path, text = root / rel, _read(root / rel)
        if foreign(text):
            refused.append(rel)
            continue
        with path.open("w", encoding="utf-8", newline="") as f:
            f.write(with_header(text, headers[Path(rel).suffix]))
    return refused


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check, or write, the copyright header (ADR-102).")
    parser.add_argument("--fix", action="store_true", help="write the header into every file without it")
    if parser.parse_args(argv).fix:
        refused = fix()
        for rel in refused:
            print(f"left alone, a copyright is already near its top (decide by hand): {rel}")
        return 1 if refused else 0
    missing = unheaded()
    annotate = os.environ.get("GITHUB_ACTIONS") == "true"
    for rel in missing:
        if annotate:
            print(
                f"::error file={rel},line=1::No copyright header. `just copyright-fix` writes it (COPYRIGHT.txt)."
            )
        else:
            print(f"no copyright header: {rel}")
    if missing:
        print(
            f"{len(missing)} file(s) without the copyright header; `just copyright-fix` writes it",
            file=sys.stderr,
        )
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
