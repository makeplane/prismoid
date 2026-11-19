/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import type { CollectionField } from "~/lib/collection";
import { clampColW, COL_MAX, COL_MIN, DEFAULT_COL_PX, resolveWidths } from "./widths";

const f = (key: string, type: CollectionField["type"]): CollectionField => ({
  key,
  label: key,
  type,
  options: [],
  unit: "",
});

describe("clampColW", () => {
  it("rounds and clamps into [80, 800]", () => {
    expect(clampColW(12)).toBe(COL_MIN);
    expect(clampColW(4000)).toBe(COL_MAX);
    expect(clampColW(240.6)).toBe(241);
  });
});

describe("resolveWidths", () => {
  const fields = [f("name", "text"), f("cost", "number")];

  it("resolves live > persisted > type default, per column", () => {
    expect(resolveWidths(fields, undefined)).toEqual({ name: 260, cost: 112 });
    expect(resolveWidths(fields, { name: 320 })).toEqual({ name: 320, cost: 112 });
    expect(resolveWidths(fields, { name: 320 }, { name: 400 })).toEqual({ name: 400, cost: 112 });
  });

  it("clamps persisted values and lets non-finite ones fall to the type default", () => {
    expect(resolveWidths(fields, { name: 5000 }).name).toBe(COL_MAX);
    expect(resolveWidths(fields, { name: Number.NaN }).name).toBe(DEFAULT_COL_PX.text);
  });
});
