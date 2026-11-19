/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The view-config engine (Collections v3): one pure projection every lens consumes.
 *
 * `applyView` is the consistency spine - filter, then sort, then visible fields - so a filter
 * set in the toolbar means the same thing on the board, the table, the gantt, and the peek's
 * ↑/↓ order. React-free like the rest of core/; the op semantics below are total per field
 * type, so the tests are mechanical.
 */

import {
  cellDateMs,
  cellNumber,
  cellString,
  fieldOf,
  primaryField,
  sortRows,
  type Collection,
  type CollectionField,
  type CollectionRow,
  type CollectionView,
  type FilterSpec,
} from "~/lib/collection";

export type Projected = { rows: CollectionRow[]; fields: CollectionField[] };

export function applyView(data: Collection, view: CollectionView): Projected {
  return {
    rows: sortRows(data, filterRows(data, data.rows, view.filter), view.sort),
    fields: visibleFields(data, view),
  };
}

/** `view.columns` resolved in order; unknown keys dropped; [] (or all-unknown) ⇒ every field.
 * One list drives ALL layouts: table columns, card chips, peek emphasis. */
export function visibleFields(data: Collection, view: CollectionView): CollectionField[] {
  const picked = view.columns
    .map((key) => fieldOf(data, key))
    .filter((f): f is CollectionField => f != null);
  return picked.length > 0 ? picked : data.fields;
}

/** The chips a card carries: visible fields minus the primary and the keys the layout already
 * shows structurally (board group, gallery cover). Replaces the old auto-picked metaFields. */
export function cardFields(
  data: Collection,
  view: CollectionView,
  exclude: (string | null | undefined)[] = [],
): CollectionField[] {
  const skip = new Set([primaryField(data).key, ...exclude.filter((k) => k != null)]);
  return visibleFields(data, view).filter((f) => !skip.has(f.key));
}

export function filterRows(
  data: Collection,
  rows: CollectionRow[],
  specs: FilterSpec[],
): CollectionRow[] {
  if (specs.length === 0) return rows;
  return rows.filter((row) =>
    specs.every((spec) => {
      const field = fieldOf(data, spec.field);
      // A dangling spec (its field vanished mid-rebase) is ignored - never hide every row.
      return field == null ? true : matchesFilter(field, row.cells[spec.field], spec);
    }),
  );
}

const isAbsent = (cell: unknown): boolean =>
  cell == null || cell === "" || (Array.isArray(cell) && cell.length === 0);

export function matchesFilter(field: CollectionField, cell: unknown, spec: FilterSpec): boolean {
  switch (spec.op) {
    case "empty":
      return isAbsent(cell);
    case "not_empty":
      return !isAbsent(cell);
    case "eq":
      return eq(field, cell, spec.value);
    case "ne":
      // "is not X" includes blanks - an absent cell is not X (Notion semantics).
      return !eq(field, cell, spec.value);
    case "contains": {
      if (isAbsent(cell)) return false;
      const needle = String(spec.value ?? "").toLowerCase();
      if (field.type === "multi_select") {
        return Array.isArray(cell) && cell.some((v) => String(v).toLowerCase() === needle);
      }
      return (cellString(cell) ?? "").toLowerCase().includes(needle);
    }
    case "gte":
    case "lte": {
      if (isAbsent(cell)) return false;
      const cmp = compare(field, cell, spec.value);
      return cmp == null ? false : spec.op === "gte" ? cmp >= 0 : cmp <= 0;
    }
  }
}

function eq(field: CollectionField, cell: unknown, value: FilterSpec["value"]): boolean {
  if (isAbsent(cell)) return false;
  if (field.type === "number") {
    const a = cellNumber(cell);
    return a != null && a === cellNumber(value);
  }
  if (field.type === "date") {
    const a = cellDateMs(cell);
    return a != null && a === cellDateMs(value);
  }
  if (field.type === "checkbox") return cell === value;
  return (cellString(cell) ?? "") === (cellString(value) ?? "");
}

/** cell − value as a sign, or null when either side is unreadable in the field's own scale. */
function compare(field: CollectionField, cell: unknown, value: FilterSpec["value"]): number | null {
  if (field.type === "number" || field.type === "date") {
    const read = field.type === "number" ? cellNumber : cellDateMs;
    const a = read(cell);
    const b = read(value);
    return a == null || b == null ? null : a - b;
  }
  return String(cellString(cell) ?? "").localeCompare(String(cellString(value) ?? ""));
}
