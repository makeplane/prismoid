# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The router reads only a skill's description, so a description that contradicts the skill is a
misroute waiting to happen (routing evals, clean main and after):

- `assemble` ships the ADR-029 walkers (`md_to_pptx.py`, `md_to_xlsx.py`) and carries routing cases
  for them, but its description said `.docx, .pdf, or .md` and "Not for: ... decks, or spreadsheets" -
  so "assemble the slide-group sections into the final deck" went to `deck`, the sheet-spec join to
  `model`.
- `summarize` vs `report` on "summarise this brief into a short written report": the deliverable
  decides, and the description now says so."""

from __future__ import annotations

from prismoid.ai.skills import format as fmt
from prismoid.operations.skills import BUILTIN_DIR


def _description(name: str) -> str:
    return fmt.parse_dir(BUILTIN_DIR / name).manifest.description


def test_assemble_says_it_joins_decks_and_workbooks_too() -> None:
    said = _description("assemble")
    assert ".pptx" in said and ".xlsx" in said
    boundary = said.split("Not for:")[1]
    assert "decks, or spreadsheets" not in boundary
    assert all(
        (BUILTIN_DIR / "assemble" / "scripts" / s).exists() for s in ("md_to_pptx.py", "md_to_xlsx.py")
    )


def test_summarize_hands_a_report_deliverable_to_report() -> None:
    assert "even one that summarises" in _description("summarize")
