# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""plane_house - the house look, baked into the frame image (ADR-034).

One visual identity for every binary deliverable: `tokens` (palette, type scale, margins),
`pptx_theme.HouseDeck` (16:9 decks with real layouts), `docx_theme` (Word files with a cover,
page numbers, proper tables and numbered figures), `pdf_theme` (the reportlab fallback twin),
`render` (deliverable → PDF → page PNGs, read-only), and `geometry` (the layout checks the QA
gate and the author-side pre-flight both run). Pure modules import nothing from the frame's
document libraries at module level, so `tokens`/`split`/`geometry`'s helpers are unit-testable
in the worker venv; everything parser-bound is imported lazily inside the functions.

Lives at /opt/plane in the image (PYTHONPATH), root-owned and read-only: guidance the system
gives the agent, never the agent's workshop.
"""

from __future__ import annotations

from plane_house.tokens import HOUSE, Theme

__all__ = ["HOUSE", "Theme"]
