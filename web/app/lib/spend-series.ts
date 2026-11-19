/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Pure shaping for the admin spend charts: SpendPointOut[] (day buckets, optionally keyed by
// model ref or user email) → aligned per-key series and SVG point strings. No DOM, no dates
// beyond string sort (day keys are ISO-shaped, so string order IS chronology) - trivially
// testable, and the routes only ever draw what these return.

import type { components } from "./api/types.gen";

/** The wire row, not a hand-kept copy of it - a parallel type is a drift waiting to happen. */
export type SpendPoint = components["schemas"]["SpendPointOut"];

export type SpendSeries = {
  /** Every day present in the window, ascending. */
  days: string[];
  /** One aligned value-per-day array per key, ranked by total spend (descending). Keys past
   *  `maxKeys` fold into one "other" series so a busy workspace can't mint forty chart lines. */
  series: { key: string; values: number[]; total: number }[];
  /**
   * The two y-ceilings, because there are two ways to draw this and they do not share one.
   *
   * `stackedMax` - the tallest day once every key is summed. Correct for a view that shows the
   * day's TOTAL (the ungrouped bars), and correct as the "peak/day" a reader is told about.
   *
   * `seriesMax` - the tallest single (key, day) value. Correct for UNSTACKED lines, which is
   * what a multi-key view actually draws: scaling those against the stacked ceiling divided
   * every line by the number of keys, so with eight models nothing came near the top and the
   * biggest spender looked like a rounding error.
   */
  stackedMax: number;
  seriesMax: number;
};

const num = (v: string): number => {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
};

/** Fold day buckets into aligned per-key series. Ungrouped points (key null) land under
 *  "total". Missing days are zero-filled so every series spans the same x axis. */
export function buildSpendSeries(points: SpendPoint[], maxKeys = 8): SpendSeries {
  const days = [...new Set(points.map((p) => p.day))].sort();
  const dayIndex = new Map(days.map((d, i) => [d, i]));
  const byKey = new Map<string, number[]>();
  for (const p of points) {
    const key = p.key ?? "total";
    let values = byKey.get(key);
    if (!values) {
      values = days.map(() => 0);
      byKey.set(key, values);
    }
    values[dayIndex.get(p.day) ?? 0] += num(p.cost_usd);
  }
  const ranked = [...byKey.entries()]
    .map(([key, values]) => ({
      key,
      values,
      total: values.reduce((a, b) => a + b, 0),
    }))
    .sort((a, b) => b.total - a.total);
  const series = ranked.slice(0, maxKeys);
  const rest = ranked.slice(maxKeys);
  if (rest.length > 0) {
    const values = days.map((_, i) => rest.reduce((sum, s) => sum + s.values[i], 0));
    series.push({
      key: "other",
      values,
      total: values.reduce((a, b) => a + b, 0),
    });
  }
  // Both ceilings, computed once over the same aligned arrays. Every series still shares ONE
  // axis - that part was always right; what was wrong was WHICH axis the unstacked lines got.
  const stackedMax = days.length
    ? Math.max(...days.map((_, i) => series.reduce((sum, s) => sum + s.values[i], 0)))
    : 0;
  const seriesMax = series.length ? Math.max(...series.flatMap((s) => s.values)) : 0;
  return { days, series, stackedMax, seriesMax };
}

/** Day totals regardless of key - the sparkline's series and the ungrouped bars. */
export function dailyTotals(points: SpendPoint[]): { day: string; value: number }[] {
  const byDay = new Map<string, number>();
  for (const p of points) {
    byDay.set(p.day, (byDay.get(p.day) ?? 0) + num(p.cost_usd));
  }
  return [...byDay.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([day, value]) => ({ day, value }));
}

/** SVG polyline points for one series inside a w×h box (y grows downward; a zero/absent max
 *  draws a flat baseline instead of dividing by zero). A single point centers itself so a
 *  one-day window still shows a mark, not an invisible zero-length line. */
export function linePoints(values: number[], max: number, w: number, h: number): string {
  if (values.length === 0) return "";
  const dx = values.length > 1 ? w / (values.length - 1) : 0;
  const x0 = values.length > 1 ? 0 : w / 2;
  return values
    .map((v, i) => {
      const y = max > 0 ? h - (v / max) * h : h;
      return `${round2(x0 + i * dx)},${round2(y)}`;
    })
    .join(" ");
}

const round2 = (n: number): number => Math.round(n * 100) / 100;
