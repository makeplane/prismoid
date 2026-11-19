/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The spend chart's pure shaping: aligned zero-filled series, rank + overflow fold, and the
// SVG point math the routes draw verbatim.

import { describe, expect, it } from "vitest";
import { buildSpendSeries, dailyTotals, linePoints, type SpendPoint } from "./spend-series";

const p = (day: string, key: string | null, usd: string): SpendPoint => ({
  day,
  key,
  cost_usd: usd,
  calls: 1,
});

describe("buildSpendSeries", () => {
  it("aligns every key to the same ascending day axis, zero-filling gaps", () => {
    const s = buildSpendSeries([
      p("2026-08-02", "a", "1.00"),
      p("2026-08-01", "a", "2.00"),
      p("2026-08-02", "b", "5.00"),
    ]);
    expect(s.days).toEqual(["2026-08-01", "2026-08-02"]);
    const a = s.series.find((x) => x.key === "a");
    const b = s.series.find((x) => x.key === "b");
    expect(a?.values).toEqual([2, 1]);
    expect(b?.values).toEqual([0, 5]); // b never spent on day one - zero, not a hole
  });

  it("ranks by total and folds the overflow into one 'other' series", () => {
    const points = [
      p("2026-08-01", "big", "10.00"),
      p("2026-08-01", "mid", "5.00"),
      p("2026-08-01", "s1", "1.00"),
      p("2026-08-01", "s2", "2.00"),
    ];
    const s = buildSpendSeries(points, 2);
    expect(s.series.map((x) => x.key)).toEqual(["big", "mid", "other"]);
    expect(s.series[2].values).toEqual([3]); // s1 + s2
  });

  it("treats ungrouped (key null) points as one 'total' series", () => {
    const s = buildSpendSeries([p("2026-08-01", null, "4.00")]);
    expect(s.series).toEqual([{ key: "total", values: [4], total: 4 }]);
    expect(s.stackedMax).toBe(4);
    expect(s.seriesMax).toBe(4); // one series: the two ceilings coincide
  });

  it("offers both ceilings, because stacked and unstacked need different ones", () => {
    const s = buildSpendSeries([
      p("2026-08-01", "a", "1.00"),
      p("2026-08-01", "b", "2.00"),
      p("2026-08-02", "a", "2.50"),
    ]);
    expect(s.stackedMax).toBe(3); // day one stacks 1+2, taller than day two's 2.5
    expect(s.seriesMax).toBe(2.5); // no single key-day is taller than a's day two
  });

  it("keeps unstacked lines legible: the tallest one reaches the ceiling", () => {
    // The bug this pins: with the stacked ceiling, eight keys meant every line topped out at
    // an eighth of the box and the biggest spender read as a rounding error.
    const points = Array.from({ length: 8 }, (_, i) => p("2026-08-01", `k${i}`, "1.00"));
    const s = buildSpendSeries(points);
    expect(s.stackedMax).toBe(8);
    expect(s.seriesMax).toBe(1);
    expect(linePoints([1], s.seriesMax, 100, 40)).toBe("50,0"); // top of the box
    expect(linePoints([1], s.stackedMax, 100, 40)).toBe("50,35"); // squashed to the floor
  });

  it("is empty-safe", () => {
    expect(buildSpendSeries([])).toEqual({
      days: [],
      series: [],
      stackedMax: 0,
      seriesMax: 0,
    });
  });
});

describe("dailyTotals", () => {
  it("sums across keys per day, ascending", () => {
    expect(
      dailyTotals([
        p("2026-08-02", "a", "1.00"),
        p("2026-08-01", "b", "2.00"),
        p("2026-08-02", "b", "0.50"),
      ]),
    ).toEqual([
      { day: "2026-08-01", value: 2 },
      { day: "2026-08-02", value: 1.5 },
    ]);
  });
});

describe("linePoints", () => {
  it("spans the box left-to-right, max at the top, zero on the baseline", () => {
    expect(linePoints([0, 5, 10], 10, 100, 40)).toBe("0,40 50,20 100,0");
  });

  it("draws a flat baseline when max is 0 instead of dividing by zero", () => {
    expect(linePoints([0, 0], 0, 100, 40)).toBe("0,40 100,40");
  });

  it("centers a single point and is empty-safe", () => {
    expect(linePoints([5], 10, 100, 40)).toBe("50,20");
    expect(linePoints([], 10, 100, 40)).toBe("");
  });
});
