# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a tool's output costs the model to read: its head AND its tail, never all of it.

A build log, a long file, a connector's page of records: the part that matters is as often at the
end (the error a build prints last, the total under a table) as at the start, and the whole of it
can fill a context window in one call. So every long result keeps its head and its tail around one
line that says how much was cut and where the whole of it is - a file under /work the model can
read a slice of, when the tool could spill one. A file is read in numbered line windows (`cat -n`,
Claude Code's Read): the model can cite a line, anchor an edit to it and read on. Pure: text in, text out.
"""

from __future__ import annotations

import re
from typing import Final

# Characters a tool result may hand the model: about three thousand tokens, head and tail halves.
OUTPUT_CAP: Final = 12_000

# A line longer than this is cut where it is shown, as Claude Code's Read and opencode's read cut one.
LINE_CAP: Final = 2_000

# Where a tool spills a long output in full, inside the agent's own scratch.
SPILL_DIR: Final = "/work/.tool-output"

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def capped(text: str, *, cap: int = OUTPUT_CAP, whole: str | None = None, read_on: str = "") -> str:
    """`text`, or its first and last `cap // 2` characters around a line naming how much was cut
    and how to see the middle, when `text` is longer than `cap`: `read_on` when the tool can read
    a slice of itself, else `whole` (where the whole of it is, for shell), else ask for less."""
    if len(text) <= cap:
        return text
    half = cap // 2
    if read_on:
        where = f" {read_on}"
    elif whole:
        where = f" The whole of it is in {whole}: read the part you need with shell (`sed -n`, `grep`)."
    else:
        where = " Ask for less (a narrower query, one page) to see the middle."
    return f"{text[:half]}\n\n[... {len(text) - 2 * half} characters cut here.{where} ...]\n\n{text[-half:]}"


def spill_path(tool: str, call_id: str) -> str:
    """The file a tool call spills its whole output to: one per call, named by the call."""
    return f"{SPILL_DIR}/{tool}-{_UNSAFE.sub('', call_id) or 'output'}.txt"


def shell_text(exit_code: int, stdout: str, stderr: str) -> str:
    """A command's result as the model reads it: the exit code when it failed, stderr first (the
    reason a build failed leads), then stdout."""
    out, err = stdout.strip(), stderr.strip()
    parts = [f"[exit {exit_code}]"] if exit_code != 0 else []
    if err:
        parts.append(f"[stderr]\n{err}")
    if out:
        parts.append(f"[stdout]\n{out}" if err else out)
    if not out and not err:
        parts.append("(no output)" if exit_code != 0 else "(exit 0, no output)")
    return "\n".join(parts)


def numbered(text: str, offset: int = 1, limit: int | None = None, *, cap: int = OUTPUT_CAP) -> str:
    """`text`'s lines from `offset` (1-based) as `cat -n` numbers them, as many as `limit` and `cap` allow,
    and, unless that is the whole file, a closing line saying which lines they were and where to read on."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    total, first = len(lines), max(1, offset)
    if first > total:
        return f"[the file has {total} lines: offset={offset} is past its end]" if total else "(empty file)"
    stop = total if limit is None else min(total, first - 1 + max(1, limit))
    shown: list[str] = []
    size = 0
    for n in range(first, stop + 1):
        line = lines[n - 1].removesuffix("\r")
        row = f"{n:6}\t{line[:LINE_CAP]}{' [... line cut]' if len(line) > LINE_CAP else ''}"
        if shown and size + len(row) + 1 > cap:
            break
        shown.append(row)
        size += len(row) + 1
    last = first + len(shown) - 1
    if first == 1 and last == total:
        return "\n".join(shown)
    after = f"read on with offset={last + 1}" if last < total else "the end of the file"
    return "\n".join(shown) + f"\n\n[lines {first} to {last} of {total}: {after}]"
