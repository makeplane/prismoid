/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import type { Collection, FilterSpec } from "~/lib/collection";
import { parseCollection } from "~/lib/collection";
import { FIXTURE_TEXT } from "../collection.fixtures";
import { applyView, cardFields, filterRows, matchesFilter, visibleFields } from "./view-config";

const DATA = (parseCollection(FIXTURE_TEXT) as { ok: true; data: Collection }).data;
const view = (name: string) => DATA.views.find((v) => v.name === name)!;
const ids = (rows: { id: string }[]) => rows.map((r) => r.id);
const F = (
  fieldKey: string,
  op: FilterSpec["op"],
  value?: string | number | boolean | null,
): FilterSpec => ({ field: fieldKey, op, value });

describe("filterRows - the op matrix", () => {
  it("eq / ne on a select; absent cells MATCH ne (is-not includes blanks)", () => {
    expect(ids(filterRows(DATA, DATA.rows, [F("stage", "eq", "Doing")]))).toEqual(["r1"]);
    expect(ids(filterRows(DATA, DATA.rows, [F("stage", "ne", "Doing")]))).toEqual([
      "r2",
      "r3",
      "r4",
    ]);
  });

  it("contains is case-insensitive substring on text", () => {
    expect(ids(filterRows(DATA, DATA.rows, [F("name", "contains", "HIRING")]))).toEqual(["r2"]);
  });

  it("gte/lte read numbers and dates in their own scales; absent cells are excluded", () => {
    expect(ids(filterRows(DATA, DATA.rows, [F("cost", "gte", 40)]))).toEqual(["r1"]);
    expect(ids(filterRows(DATA, DATA.rows, [F("due", "lte", "2026-08-14")]))).toEqual(["r1", "r3"]);
  });

  it('empty / not_empty treat null, "" and [] as absent', () => {
    expect(ids(filterRows(DATA, DATA.rows, [F("due", "empty")]))).toEqual(["r4"]);
    expect(ids(filterRows(DATA, DATA.rows, [F("due", "not_empty")]))).toEqual(["r1", "r2", "r3"]);
  });

  it("checkbox eq is strict boolean", () => {
    expect(ids(filterRows(DATA, DATA.rows, [F("shipped", "eq", true)]))).toEqual(["r3"]);
  });

  it("a dangling field spec is ignored - it never hides every row", () => {
    expect(filterRows(DATA, DATA.rows, [F("ghost", "eq", "x")])).toHaveLength(4);
  });

  it("multi_select contains matches whole options, case-insensitively", () => {
    const tags = {
      key: "tags",
      label: "Tags",
      type: "multi_select" as const,
      options: ["infra", "gtm"],
      unit: "",
    };
    expect(matchesFilter(tags, ["infra"], F("tags", "contains", "Infra"))).toBe(true);
    expect(matchesFilter(tags, ["gtm"], F("tags", "contains", "infra"))).toBe(false);
    expect(matchesFilter(tags, [], F("tags", "empty"))).toBe(true);
  });
});

describe("visibleFields / cardFields", () => {
  it("resolves columns in order and drops unknown keys", () => {
    expect(visibleFields(DATA, view("All")).map((f) => f.key)).toEqual([
      "name",
      "stage",
      "owner",
      "due",
      "cost",
    ]);
    const odd = { ...view("All"), columns: ["cost", "ghost", "name"] };
    expect(visibleFields(DATA, odd).map((f) => f.key)).toEqual(["cost", "name"]);
  });

  it("falls back to every field on [] or all-unknown columns", () => {
    expect(visibleFields(DATA, view("Pipeline"))).toHaveLength(DATA.fields.length);
    const ghost = { ...view("All"), columns: ["ghost"] };
    expect(visibleFields(DATA, ghost)).toHaveLength(DATA.fields.length);
  });

  it("cardFields drops the primary and the structurally-shown keys", () => {
    expect(cardFields(DATA, view("All")).map((f) => f.key)).toEqual([
      "stage",
      "owner",
      "due",
      "cost",
    ]);
    expect(cardFields(DATA, view("All"), ["stage"]).map((f) => f.key)).toEqual([
      "owner",
      "due",
      "cost",
    ]);
  });
});

describe("applyView - filter, then sort, then fields: one spine for every lens", () => {
  it("composes", () => {
    const lens = {
      ...view("All"),
      filter: [F("stage", "ne", "Done")],
      sort: [{ field: "due", direction: "desc" as const }],
    };
    const { rows, fields } = applyView(DATA, lens);
    // r3 is Done (filtered out); r4 has no due, and absent sinks regardless of direction.
    expect(ids(rows)).toEqual(["r2", "r1", "r4"]);
    expect(fields.map((f) => f.key)).toEqual(["name", "stage", "owner", "due", "cost"]);
  });
});
