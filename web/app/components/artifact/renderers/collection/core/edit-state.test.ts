/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import type { Collection } from "~/lib/collection";
import { parseCollection } from "~/lib/collection";
import { FIXTURE_TEXT } from "../collection.fixtures";
import {
  applyEdit,
  buildContent,
  invertEdit,
  mintRowId,
  moveRowEdit,
  pushEdit,
  type Edit,
} from "./edit-state";

const BASE = (parseCollection(FIXTURE_TEXT) as { ok: true; data: Collection }).data;

describe("applyEdit", () => {
  it("set_cell rewrites one cell and nothing else", () => {
    const next = applyEdit(BASE, {
      kind: "set_cell",
      rowId: "r2",
      key: "stage",
      value: "Doing",
      prev: "Todo",
    });
    expect(next.rows.find((r) => r.id === "r2")?.cells.stage).toBe("Doing");
    expect(next.rows.find((r) => r.id === "r1")?.cells.stage).toBe("Doing");
    expect(BASE.rows.find((r) => r.id === "r2")?.cells.stage).toBe("Todo"); // base untouched
  });

  it("add/remove/move keep the row set coherent", () => {
    const added = applyEdit(BASE, {
      kind: "add_row",
      row: { id: "r9", cells: { name: "New" } },
      index: 1,
    });
    expect(added.rows[1].id).toBe("r9");
    const removed = applyEdit(added, { kind: "remove_row", row: added.rows[1], index: 1 });
    expect(removed.rows.map((r) => r.id)).toEqual(BASE.rows.map((r) => r.id));
    const moved = applyEdit(BASE, { kind: "move_row", rowId: "r4", from: 3, to: 0 });
    expect(moved.rows[0].id).toBe("r4");
  });
});

describe("invertEdit", () => {
  it("every command round-trips through its inverse", () => {
    const edits: Edit[] = [
      { kind: "set_cell", rowId: "r1", key: "stage", value: "Done", prev: "Doing" },
      { kind: "add_row", row: { id: "r9", cells: {} }, index: 4 },
      { kind: "move_row", rowId: "r1", from: 0, to: 2 },
    ];
    for (const edit of edits) {
      const there = applyEdit(BASE, edit);
      const back = applyEdit(there, invertEdit(edit));
      expect(back.rows).toEqual(BASE.rows);
    }
  });
});

describe("buildContent (the save payload and the rebase engine)", () => {
  it("replays the same commands over a NEWER base - staged edits survive a 409", () => {
    const edits: Edit[] = [
      { kind: "set_cell", rowId: "r2", key: "stage", value: "Doing", prev: "Todo" },
    ];
    // Someone else renamed r2 meanwhile; our stage edit lands on top, their rename survives.
    const newer = applyEdit(BASE, {
      kind: "set_cell",
      rowId: "r2",
      key: "name",
      value: "Field hiring (EU)",
      prev: "Field-engineer hiring",
    });
    const rebased = buildContent(newer, edits);
    const r2 = rebased.rows.find((r) => r.id === "r2");
    expect(r2?.cells.stage).toBe("Doing");
    expect(r2?.cells.name).toBe("Field hiring (EU)");
  });
});

describe("mintRowId", () => {
  it("never collides with existing ids", () => {
    const id = mintRowId(BASE);
    expect(BASE.rows.some((r) => r.id === id)).toBe(false);
  });
});

describe("view commands (add/remove a lens on the fly)", () => {
  const newView: Collection["views"][number] = {
    name: "Board 2",
    layout: "board",
    group_by: "stage",
    sub_group_by: null,
    sort: [],
    filter: [],
    date_field: null,
    start_field: null,
    end_field: null,
    cover_field: null,
    columns: [],
    widths: {},
    column_width: null,
  };

  it("add_view appends and remove_view removes by name; both invert cleanly", () => {
    const added = applyEdit(BASE, { kind: "add_view", view: newView, index: BASE.views.length });
    expect(added.views.map((v) => v.name)).toContain("Board 2");
    const removed = applyEdit(added, {
      kind: "remove_view",
      view: newView,
      index: BASE.views.length,
    });
    expect(removed.views.map((v) => v.name)).toEqual(BASE.views.map((v) => v.name));

    const edit = { kind: "add_view", view: newView, index: 1 } satisfies Edit;
    const back = applyEdit(applyEdit(BASE, edit), invertEdit(edit));
    expect(back.views).toEqual(BASE.views);
  });
});

describe("update_view (toolbar and width changes)", () => {
  const grid = BASE.views[1]; // "All", the table lens
  const widen = (prev: typeof grid, widths: Record<string, number>): Edit => ({
    kind: "update_view",
    index: 1,
    prev,
    next: { ...prev, widths },
  });

  it("swaps one view by prev.name; the inverse restores it", () => {
    const edit = widen(grid, { name: 480 });
    const there = applyEdit(BASE, edit);
    expect(there.views[1].widths).toEqual({ name: 480 });
    expect(there.views[0]).toBe(BASE.views[0]); // untouched views keep identity
    expect(applyEdit(there, invertEdit(edit)).views).toEqual(BASE.views);
  });

  it("rename chains - a later edit carries the new name as its prev", () => {
    const renamed = { ...grid, name: "Grid" };
    const out = buildContent(BASE, [
      { kind: "update_view", index: 1, prev: grid, next: renamed },
      widen(renamed, { name: 500 }),
    ]);
    expect(out.views[1].name).toBe("Grid");
    expect(out.views[1].widths).toEqual({ name: 500 });
  });

  it("no-ops silently when the view vanished meanwhile (rebase last-write-wins)", () => {
    const gone = { ...grid, name: "Gone" };
    expect(applyEdit(BASE, widen(gone, { name: 500 })).views).toEqual(BASE.views);
  });
});

describe("pushEdit coalescing (a drag is ONE undo step)", () => {
  const grid = BASE.views[1];
  const at = (w: number, from = grid): Edit => ({
    kind: "update_view",
    index: 1,
    prev: from,
    next: { ...grid, widths: { name: w } },
  });

  it("merges a width burst, keeping the ORIGINAL prev and the LATEST next", () => {
    let edits = pushEdit([], at(340), true);
    edits = pushEdit(edits, at(360, { ...grid, widths: { name: 340 } }), true);
    edits = pushEdit(edits, at(380, { ...grid, widths: { name: 360 } }), true);
    expect(edits).toHaveLength(1);
    const only = edits[0];
    if (only.kind !== "update_view") throw new Error("expected update_view");
    expect(only.prev).toEqual(grid);
    expect(only.next.widths).toEqual({ name: 380 });
    // One undo restores the original width.
    expect(applyEdit(applyEdit(BASE, only), invertEdit(only)).views).toEqual(BASE.views);
  });

  it("stages plainly without coalesce and never merges across views", () => {
    const two = pushEdit(
      pushEdit([], at(340), false),
      at(360, { ...grid, widths: { name: 340 } }),
      false,
    );
    expect(two).toHaveLength(2);

    const board = BASE.views[0];
    const boardEdit: Edit = {
      kind: "update_view",
      index: 0,
      prev: board,
      next: { ...board, column_width: 400 },
    };
    expect(pushEdit(pushEdit([], at(340), true), boardEdit, true)).toHaveLength(2);
  });
});

describe("batch (one gesture, one undo step)", () => {
  const move: Edit = {
    kind: "batch",
    label: "Move bar",
    edits: [
      { kind: "set_cell", rowId: "r1", key: "start", value: "2026-07-04", prev: "2026-07-01" },
      { kind: "set_cell", rowId: "r1", key: "due", value: "2026-08-17", prev: "2026-08-14" },
    ],
  };

  it("applies children in order and round-trips through its inverse", () => {
    const there = applyEdit(BASE, move);
    expect(there.rows.find((r) => r.id === "r1")?.cells.start).toBe("2026-07-04");
    expect(there.rows.find((r) => r.id === "r1")?.cells.due).toBe("2026-08-17");
    expect(applyEdit(there, invertEdit(move)).rows).toEqual(BASE.rows);
  });

  it("mixes command kinds (set_cell + move_row) and still round-trips", () => {
    const mixed: Edit = {
      kind: "batch",
      edits: [
        { kind: "set_cell", rowId: "r1", key: "stage", value: "Done", prev: "Doing" },
        { kind: "move_row", rowId: "r1", from: 0, to: 2 },
      ],
    };
    const there = applyEdit(BASE, mixed);
    expect(there.rows[2].id).toBe("r1");
    expect(there.rows[2].cells.stage).toBe("Done");
    expect(applyEdit(there, invertEdit(mixed)).rows).toEqual(BASE.rows);
  });

  it("an empty batch is an identity no-op", () => {
    expect(applyEdit(BASE, { kind: "batch", edits: [] })).toBe(BASE);
  });

  it("never coalesces with a neighboring update_view", () => {
    const grid = BASE.views[1];
    const uv: Edit = {
      kind: "update_view",
      index: 1,
      prev: grid,
      next: { ...grid, widths: { name: 400 } },
    };
    expect(pushEdit(pushEdit([], uv, true), move, true)).toHaveLength(2);
  });

  it("inverts with children reversed", () => {
    const inv = invertEdit(move);
    if (inv.kind !== "batch") throw new Error("expected batch");
    expect(inv.edits.map((e) => (e.kind === "set_cell" ? e.key : ""))).toEqual(["due", "start"]);
  });
});

describe("moveRowEdit (visible-neighbor anchoring)", () => {
  // BASE rows: [r1, r2, r3, r4]
  it("beforeId places before the anchor; afterId places after", () => {
    const before = moveRowEdit(BASE.rows, "r4", { beforeId: "r2" });
    expect(before).toEqual({ kind: "move_row", rowId: "r4", from: 3, to: 1 });
    const after = moveRowEdit(BASE.rows, "r1", { afterId: "r3" });
    expect(after).toEqual({ kind: "move_row", rowId: "r1", from: 0, to: 2 });
  });

  it("returns null on unchanged order and on vanished anchors", () => {
    expect(moveRowEdit(BASE.rows, "r2", { beforeId: "r3" })).toBeNull(); // already there
    expect(moveRowEdit(BASE.rows, "r2", { afterId: "r1" })).toBeNull(); // already there
    expect(moveRowEdit(BASE.rows, "r2", { beforeId: "ghost" })).toBeNull();
    expect(moveRowEdit(BASE.rows, "ghost", { beforeId: "r1" })).toBeNull();
  });

  it("keeps hidden rows in place - the projection invariant", () => {
    // Document [A,h1,h2,B,C]; the view shows [A,B,C]; the user drags C before B.
    const doc = [{ id: "a" }, { id: "h1" }, { id: "h2" }, { id: "b" }, { id: "c" }];
    const edit = moveRowEdit(doc, "c", { beforeId: "b" })!;
    const rows = [...doc];
    const [moved] = rows.splice(
      rows.findIndex((r) => r.id === "c"),
      1,
    );
    rows.splice(edit.to, 0, moved);
    expect(rows.map((r) => r.id)).toEqual(["a", "h1", "h2", "c", "b"]);

    // Drop at the visible end anchors after the last VISIBLE row, never the document end.
    const tail = [{ id: "a" }, { id: "b" }, { id: "h3" }];
    const endEdit = moveRowEdit(tail, "a", { afterId: "b" })!;
    const t = [...tail];
    const [m] = t.splice(
      t.findIndex((r) => r.id === "a"),
      1,
    );
    t.splice(endEdit.to, 0, m);
    expect(t.map((r) => r.id)).toEqual(["b", "a", "h3"]);
  });
});

describe("update_view rename (names are the targeting key)", () => {
  it("renames in place, chains onto the new name, and inverts back", () => {
    const view = BASE.views[0];
    const rename: Edit = {
      kind: "update_view",
      index: 0,
      prev: view,
      next: { ...view, name: "Pipeline 2.0" },
    };
    const renamed = applyEdit(BASE, rename);
    expect(renamed.views[0].name).toBe("Pipeline 2.0");
    expect(renamed.views).toHaveLength(BASE.views.length);

    // A later edit must target the NEW name (prev carries it) - the chaining law.
    const chained = applyEdit(renamed, {
      kind: "update_view",
      index: 0,
      prev: renamed.views[0],
      next: { ...renamed.views[0], group_by: undefined },
    });
    expect(chained.views[0].name).toBe("Pipeline 2.0");

    const undone = applyEdit(renamed, invertEdit(rename));
    expect(undone.views[0].name).toBe(view.name);
  });
});
