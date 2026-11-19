# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Density arithmetic - pure, stdlib, unit-tested in the worker venv (ADR-034).

A slide that needs more than the caps is two slides; a text box that cannot hold its text at
the house size is a defect, never a smaller font. These helpers decide both without opening a
document library.
"""

from __future__ import annotations

# Average glyph width as a fraction of the point size for a humanist sans at body sizes -
# Calibri/Carlito sit near 0.48; 0.5 errs toward flagging, which is the right side to err.
_GLYPH_WIDTH_EM = 0.5
_LEADING = 1.2  # line height as a multiple of the point size


def split_bullets(items: list[str], max_bullets: int, max_words: int) -> list[list[str]]:
    """Chunk bullets so no chunk exceeds EITHER cap, and BALANCE the chunks: eight bullets
    become 4+4, never 6+2 (a sparse continuation slide reads as a mistake). Never drops an
    item; a single bullet longer than `max_words` stands alone (the writer's problem to
    shorten, not ours to truncate). Empty input → []."""
    if not items:
        return []
    greedy = _greedy(items, max_bullets, max_words)
    n_chunks = len(greedy)
    if n_chunks == 1:
        return greedy
    # Re-cut into the same NUMBER of chunks with near-equal counts, then re-check the caps;
    # if balancing breaks a cap (very uneven word loads), the greedy cut stands.
    size = -(-len(items) // n_chunks)
    balanced = [items[i : i + size] for i in range(0, len(items), size)]
    if all(len(c) <= max_bullets and sum(len(i.split()) for i in c) <= max_words for c in balanced):
        return balanced
    return greedy


def _greedy(items: list[str], max_bullets: int, max_words: int) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    words = 0
    for item in items:
        n = len(item.split())
        if current and (len(current) >= max_bullets or words + n > max_words):
            chunks.append(current)
            current, words = [], 0
        current.append(item)
        words += n
    if current:
        chunks.append(current)
    return chunks


def lines_needed(text: str, box_w_in: float, pt: float) -> int:
    """How many wrapped lines `text` takes in a box `box_w_in` wide at `pt` points. Explicit
    newlines count; each paragraph wraps independently."""
    chars_per_line = max(1, int((box_w_in * 72) / (pt * _GLYPH_WIDTH_EM)))
    total = 0
    for para in text.split("\n"):
        total += max(1, -(-len(para) // chars_per_line))  # ceil division
    return total


def estimate_overflow(text: str, box_w_in: float, box_h_in: float, pt: float) -> bool:
    """True when `text` at `pt` points will not fit inside the box. A conservative model -
    it flags a little early rather than a little late, because the cost of a false flag is a
    split slide and the cost of a miss is text running off a customer's screen."""
    if not text.strip():
        return False
    needed_in = lines_needed(text, box_w_in, pt) * pt * _LEADING / 72
    return needed_in > box_h_in


def fit_box(
    img_w: int, img_h: int, box: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    """Aspect-fit an image of `img_w×img_h` pixels inside `box=(left, top, w, h)` inches,
    centred. Returns the placed (left, top, w, h). A degenerate image fills the box."""
    left, top, bw, bh = box
    if img_w <= 0 or img_h <= 0:
        return box
    scale = min(bw / img_w, bh / img_h)
    w, h = img_w * scale, img_h * scale
    return (left + (bw - w) / 2, top + (bh - h) / 2, w, h)
