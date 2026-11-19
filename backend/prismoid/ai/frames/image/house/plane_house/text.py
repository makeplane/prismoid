# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Pure text helpers the three builders share (ADR-034): what counts as a number (for
right-aligned columns), which columns are numeric, and a caption with the house's own figure
number stripped. Stdlib only - unit-tested in the worker venv."""

from __future__ import annotations

import re

_NUMBER = re.compile(r"[-+$€£(]?\s?[\d,]+(\.\d+)?\s?[%)xX]?[KMBkmb]?")
_FIGURE_PREFIX = re.compile(r"^\s*(figure|fig\.?)\s*\d+\s*[.:\-–\u2014]\s*", re.I)


def is_number(text: str) -> bool:
    return bool(_NUMBER.fullmatch(text.strip()))


def numeric_columns(rows: list[list[str]], cols: int) -> list[bool]:
    """Per column: every NON-EMPTY body cell is a number AND at least one exists - an empty
    column is not numeric (the vacuous `all()` right-aligned its header)."""
    out: list[bool] = []
    for c in range(cols):
        cells = [r[c].strip() for r in rows[1:] if c < len(r) and r[c].strip()]
        out.append(bool(cells) and all(is_number(v) for v in cells))
    return out


def bare_caption(caption: str) -> str:
    """A caller who already wrote "Figure 2. …" must not get "Figure 2. Figure 2. …" (seen live,
    task 806c1069): the number is the house's to write."""
    return _FIGURE_PREFIX.sub("", caption)


def figure_caption(number: int, caption: str, *, pending: bool = False) -> str:
    body = bare_caption(caption).strip()
    text = f"Figure {number}. {body}" if body else f"Figure {number}"
    return f"{text} [figure pending]" if pending else text
