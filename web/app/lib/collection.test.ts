/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import {
  FIXTURE,
  FIXTURE_TEXT,
} from "~/components/artifact/renderers/collection/collection.fixtures";
import {
  cellDateMs,
  cellString,
  groupRows,
  parseCollection,
  primaryField,
  sortRows,
  type Collection,
} from "./collection";

const DATA = (parseCollection(FIXTURE_TEXT) as { ok: true; data: Collection }).data;

describe("parseCollection", () => {
  it("accepts the fixture", () => {
    const parsed = parseCollection(FIXTURE_TEXT);
    expect(parsed.ok).toBe(true);
  });

  it("rejects non-JSON with a readable reason", () => {
    const parsed = parseCollection("not json");
    expect(parsed).toEqual({ ok: false, reason: "The file is not JSON." });
  });

  it("gates on collection_version - v2 bytes are an honest refusal, not a guess", () => {
    const parsed = parseCollection(JSON.stringify({ ...FIXTURE, collection_version: 2 }));
    expect(parsed.ok).toBe(false);
    if (!parsed.ok) expect(parsed.reason).toContain("collection_version");
  });

  it("keeps an unknown layout - degrading is the renderer's job, rejecting is nobody's", () => {
    const doc = { ...FIXTURE, views: [{ name: "New", layout: "starmap" }] };
    const parsed = parseCollection(JSON.stringify(doc));
    expect(parsed.ok).toBe(true);
  });
});

describe("cells", () => {
  it("coerces scalars and lists, refuses objects", () => {
    expect(cellString("x")).toBe("x");
    expect(cellString(4)).toBe("4");
    expect(cellString(true)).toBe("Yes");
    expect(cellString(["a", "b"])).toBe("a, b");
    expect(cellString({ nested: 1 })).toBeNull();
  });

  it("parses dates as UTC calendar days only", () => {
    expect(cellDateMs("2026-08-01")).toBe(Date.parse("2026-08-01T00:00:00Z"));
    expect(cellDateMs("August 1")).toBeNull();
    expect(cellDateMs(20260801)).toBeNull();
  });
});

describe("groupRows", () => {
  it("follows the select's option order and collects the ungrouped tail honestly", () => {
    const groups = groupRows(DATA, "stage");
    expect(groups.map((g) => g.label)).toEqual(["Todo", "Doing", "Done", "No Stage"]);
    expect(groups.at(-1)?.rows.map((r) => r.id)).toEqual(["r4"]);
  });
});

describe("sortRows", () => {
  it("sorts dates ascending with absent cells sinking to the end", () => {
    const sorted = sortRows(DATA, DATA.rows, [{ field: "due", direction: "asc" }]);
    expect(sorted.map((r) => r.id)).toEqual(["r3", "r1", "r2", "r4"]);
  });

  it("descending reverses values but never surfaces the holes", () => {
    const sorted = sortRows(DATA, DATA.rows, [{ field: "due", direction: "desc" }]);
    expect(sorted.map((r) => r.id)).toEqual(["r2", "r1", "r3", "r4"]);
  });
});

describe("primary field", () => {
  // Card chips are the view's visible fields now - see core/view-config's cardFields; the old
  // auto-picked metaFields is gone (it produced unlabeled "1 2" chips).
  it("leads with the first text field", () => {
    expect(primaryField(DATA).key).toBe("name");
  });
});
