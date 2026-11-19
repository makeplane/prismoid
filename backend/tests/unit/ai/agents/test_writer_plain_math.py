# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Found in the golden run (`qoq-growth`): the writer showed its working as LaTeX (`\\[ \\frac{\\$41.6\\text{M}
...}{...} \\times 100 \\]`). Nothing in the product renders LaTeX - the reader sees backslashes - and the
verifier cannot read figures out of it (`in no source: 100, 22.7, 33.9, 41.6` on a correct 22.7%). The
writer is told to show working as plain text."""

from __future__ import annotations

from prismoid.ai.agents.writer import PROMPT


def test_the_writer_shows_working_as_plain_text_never_latex() -> None:
    flat = " ".join(PROMPT.split())
    assert "never LaTeX" in flat
    assert "(41.6 - 33.9) / 33.9 × 100 = 22.7%" in flat
