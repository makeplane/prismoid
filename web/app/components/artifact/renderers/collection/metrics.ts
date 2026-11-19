/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** THE lens grid - the common variables that make six lenses feel like ONE app.
 *
 * Every lens builds from the same bones: a data row is `row` tall whether it's a table row, a
 * list row, or a gantt row; every in-lens header strip (table thead, gantt axis, calendar
 * month nav, peek header) is `strip`; every secondary strip (swimlane header, list section,
 * board column header, footers) is `subRow`/`footer`; content meets the shell edge at `pad`
 * and grids breathe at `gap`; cells pad `cellX` inside bordered grids.
 *
 * ViewShell injects these as CSS custom properties (--row-h, --strip-h, --subrow-h,
 * --footer-h), so class-based lenses write `h-(--strip-h)` and the gantt's pixel math imports
 * the SAME numbers - one source, no drift. Change a value here and every lens moves together;
 * changing one lens alone is the bug this file exists to prevent.
 *
 * Rem caveat: the app root is 17px, so a rem utility renders 6.25% over its nominal px twin
 * (p-3 = 12.75px, not 12). Box HEIGHTS on the grid are therefore always declared from these
 * px values (style height / CSS vars), never derived from rem padding - see the table rows.
 */
export const METRICS = {
  row: 44, // one data-row height across table / list / gantt
  strip: 44, // lens chrome strips: table thead, gantt axis, calendar nav, peek header
  subRow: 36, // group strips: swimlane headers, list sections, board column headers
  footer: 36, // footer strips: table footer, peek footer
  // ONE inset datum: cell text (px-3) and object-lens fields (p-3) share it, so a board
  // column's left edge sits exactly on the line where table/rail text begins. A lens is
  // either FLUSH (strips and row washes bleed to the shell edge) or it floats at pad.
  pad: 12, // p-3 - object fields: board/gallery grids, calendar body
  gap: 12, // gap-3 - intra-lens grid gaps (board columns, card grids)
  cellX: 12, // px-3 - cell/text inset inside flush lenses (table cells, gantt rail, list rows)
  gutter: 32, // the reorder-grip gutter (exact px) - table colgroup, gantt rail, list rows
  bar: 20, // inline data elements: gantt bars
  railMin: 200, // the gantt rail is the PRIMARY column - same slider grammar as the table,
  railMax: 480, // persisted in the same view.widths[primary.key] slot
  railDefault: 280,
} as const;

export const clampRail = (px: number) =>
  Math.min(METRICS.railMax, Math.max(METRICS.railMin, Math.round(px)));
