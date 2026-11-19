/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The editor's pure core (ADR-015): edits are commands, a save is `buildContent(base, edits)`.
 *
 * React-free and dnd-free on purpose - undo/redo, rebase-onto-a-newer-revision, and the revise
 * payload are all plain data transforms, unit-testable without a DOM. Every command carries what
 * its inverse needs, captured at creation time (a `set_cell` remembers the previous value), so
 * undo is a lookup, never a re-derivation.
 */

import type { Collection, CollectionRow, CollectionView } from "~/lib/collection";

export type Edit =
  | { kind: "set_cell"; rowId: string; key: string; value: unknown; prev: unknown }
  | { kind: "add_row"; row: CollectionRow; index: number }
  | { kind: "remove_row"; row: CollectionRow; index: number }
  | { kind: "move_row"; rowId: string; from: number; to: number }
  | { kind: "add_view"; view: CollectionView; index: number }
  | { kind: "remove_view"; view: CollectionView; index: number }
  | { kind: "update_view"; index: number; prev: CollectionView; next: CollectionView }
  | { kind: "batch"; label?: string; edits: Edit[] };

export function applyEdit(data: Collection, edit: Edit): Collection {
  switch (edit.kind) {
    case "set_cell":
      return {
        ...data,
        rows: data.rows.map((row) =>
          row.id === edit.rowId ? { ...row, cells: { ...row.cells, [edit.key]: edit.value } } : row,
        ),
      };
    case "add_row": {
      const rows = [...data.rows];
      rows.splice(Math.min(edit.index, rows.length), 0, edit.row);
      return { ...data, rows };
    }
    case "remove_row":
      return { ...data, rows: data.rows.filter((row) => row.id !== edit.row.id) };
    case "move_row": {
      const rows = [...data.rows];
      const from = rows.findIndex((row) => row.id === edit.rowId);
      if (from === -1) return data;
      const [moved] = rows.splice(from, 1);
      rows.splice(Math.min(edit.to, rows.length), 0, moved);
      return { ...data, rows };
    }
    case "add_view": {
      const views = [...data.views];
      views.splice(Math.min(edit.index, views.length), 0, edit.view);
      return { ...data, views };
    }
    case "remove_view":
      // By name - the document's own uniqueness key (the schema refuses duplicate view names).
      return { ...data, views: data.views.filter((v) => v.name !== edit.view.name) };
    case "update_view":
      // Matched by prev.name, the same uniqueness law as remove_view. A view renamed or removed
      // by a newer revision makes this a silent no-op on rebase - last-write-wins, the same
      // family as a set_cell whose row vanished. Renames chain: the NEXT edit's prev carries
      // the new name.
      return {
        ...data,
        views: data.views.map((v) => (v.name === edit.prev.name ? edit.next : v)),
      };
    case "batch":
      // One gesture, several commands (a bar move touches start AND end; a cross-column card
      // drop is a set_cell AND a move_row) - applied in order, undone as one.
      return edit.edits.reduce(applyEdit, data);
  }
}

export function invertEdit(edit: Edit): Edit {
  switch (edit.kind) {
    case "set_cell":
      return { ...edit, value: edit.prev, prev: edit.value };
    case "add_row":
      return { kind: "remove_row", row: edit.row, index: edit.index };
    case "remove_row":
      return { kind: "add_row", row: edit.row, index: edit.index };
    case "move_row":
      return { kind: "move_row", rowId: edit.rowId, from: edit.to, to: edit.from };
    case "add_view":
      return { kind: "remove_view", view: edit.view, index: edit.index };
    case "remove_view":
      return { kind: "add_view", view: edit.view, index: edit.index };
    case "update_view":
      return { kind: "update_view", index: edit.index, prev: edit.next, next: edit.prev };
    case "batch":
      // Reversed order is load-bearing: apply(A,B) is undone by apply(inv(B), inv(A)).
      return { kind: "batch", label: edit.label, edits: [...edit.edits].reverse().map(invertEdit) };
  }
}

export type MoveAnchor = { beforeId: string } | { afterId: string };

/** move_row targeting by VISIBLE neighbor id - THE projection-safe mapping, once. `to` is the
 * index in the post-removal array (applyEdit's splice convention). Anchors are always visible
 * neighbors, so filtered-out rows never move relative to each other and the moved row lands
 * exactly where the user saw it go. Returns null when the anchor vanished or the order is
 * unchanged - callers stage nothing. */
export function moveRowEdit(
  rows: { id: string }[],
  rowId: string,
  anchor: MoveAnchor,
): Extract<Edit, { kind: "move_row" }> | null {
  const from = rows.findIndex((r) => r.id === rowId);
  if (from === -1) return null;
  const rest = rows.filter((r) => r.id !== rowId);
  const anchorId = "beforeId" in anchor ? anchor.beforeId : anchor.afterId;
  const at = rest.findIndex((r) => r.id === anchorId);
  if (at === -1) return null;
  const to = "beforeId" in anchor ? at : at + 1;
  return to === from ? null : { kind: "move_row", rowId, from, to };
}

/** Append an edit, optionally coalescing into the previous one. A width drag or an arrow-key
 * burst is ONE gesture, so `coalesce` merges consecutive update_views on the same view - prev
 * stays the original, next becomes the latest, and a single undo restores the starting width.
 * Menu-driven changes stage plainly and remain individually undoable. */
export function pushEdit(edits: Edit[], edit: Edit, coalesce = false): Edit[] {
  const last = edits.at(-1);
  // batch is atomic: never coalesced, never a coalesce target (the guard below only ever
  // matches consecutive update_views).
  if (
    coalesce &&
    edit.kind === "update_view" &&
    last?.kind === "update_view" &&
    last.next.name === edit.prev.name
  ) {
    return [
      ...edits.slice(0, -1),
      { kind: "update_view", index: last.index, prev: last.prev, next: edit.next },
    ];
  }
  return [...edits, edit];
}

/** Base + staged edits → the exact `ReviseIn.content`. Also the rebase engine: after a 409,
 * re-run the SAME edits over the newer revision's content (cell-level last-write-wins). */
export function buildContent(base: Collection, edits: Edit[]): Collection {
  return edits.reduce(applyEdit, base);
}

/** A fresh row id that cannot collide with the working set. */
export function mintRowId(data: Collection): string {
  const taken = new Set(data.rows.map((row) => row.id));
  for (let i = data.rows.length + 1; ; i++) {
    const id = `r${i}`;
    if (!taken.has(id)) return id;
  }
}
