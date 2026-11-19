/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import { clampResizeDays, previewRange, snapDays } from "./drag";

const D = (iso: string) => Date.parse(`${iso}T00:00:00Z`);

describe("snapDays", () => {
  it("rounds pixel travel to whole days in both directions", () => {
    expect(snapDays(13, 24)).toBe(1);
    expect(snapDays(-13, 24)).toBe(-1);
    expect(snapDays(11, 24)).toBe(0);
    expect(snapDays(49, 24)).toBe(2);
  });
});

describe("clampResizeDays (the 1-day floor)", () => {
  const start = D("2026-07-01");
  const end = D("2026-07-05"); // span 5

  it("the start edge may move right only to span−1", () => {
    expect(clampResizeDays("start", 10, start, end)).toBe(4);
    expect(clampResizeDays("start", -3, start, end)).toBe(-3); // growing left is free
  });

  it("the end edge may move left only to 1−span", () => {
    expect(clampResizeDays("end", -10, start, end)).toBe(-4);
    expect(clampResizeDays("end", 6, start, end)).toBe(6);
  });

  it("a same-day bar cannot shrink at all", () => {
    expect(clampResizeDays("start", 5, start, start)).toBe(0);
    expect(clampResizeDays("end", -5, start, start)).toBe(0);
  });
});

describe("previewRange (what you see is what stages)", () => {
  const start = D("2026-07-01");
  const end = D("2026-07-05");

  it("move shifts both ends; start/end shift one edge", () => {
    expect(previewRange("move", 3, start, end)).toEqual({
      startMs: D("2026-07-04"),
      endMs: D("2026-07-08"),
    });
    expect(previewRange("start", 2, start, end)).toEqual({ startMs: D("2026-07-03"), endMs: end });
    expect(previewRange("end", -2, start, end)).toEqual({ startMs: start, endMs: D("2026-07-03") });
  });
});
