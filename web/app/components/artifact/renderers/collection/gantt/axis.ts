/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** Pure axis math for the gantt v3 windowed-infinite timeline. The viewport hook owns the DOM
 * (scroll events, ResizeObserver, compensation writes); every formula that decides WHERE
 * something lands lives here, unit-tested in node. */

import {
  DAY_MS,
  dayIndex,
  dayTicks,
  mondayOnOrBefore,
  monthSpans,
  monthTicks,
  sundayOnOrAfter,
  weekTicks,
  yearSpans,
  type MonthSpan,
  type Tick,
} from "../core/date-math";

export type Zoom = "week" | "month" | "quarter" | "year";

export const ZOOMS: Record<Zoom, { dayPx: number; label: string }> = {
  week: { dayPx: 24, label: "Week" },
  month: { dayPx: 12, label: "Month" },
  quarter: { dayPx: 6, label: "Quarter" },
  year: { dayPx: 3, label: "Year" },
};

export const EDGE_PX = 240; // scroll distance from an edge that triggers an extension
export const EXTEND_DAYS = 42; // whole weeks per extension - Monday/Sunday snapping survives
export const AXIS_CAP_DAYS = 1096; // 3 years: an honest hard edge, never a runaway loop
export const MIN_BAR_PX = 8;

export type Axis = { startMs: number; endMs: number };

/** Fit-to-data zoom: the padded span picks the coarsest preset that still shows texture. */
export function autoZoom(spanDays: number): Zoom {
  if (spanDays <= 42) return "week";
  if (spanDays <= 120) return "month";
  if (spanDays <= 365) return "quarter";
  return "year";
}

/** Data range + 3 days of air each side, snapped Monday…Sunday, then window-filled. */
export function initialAxis(
  dataMinMs: number,
  dataMaxMs: number,
  dayPx: number,
  containerW: number,
): Axis {
  const startMs = mondayOnOrBefore(dataMinMs - 3 * DAY_MS);
  const endMs = sundayOnOrAfter(dataMaxMs + 3 * DAY_MS);
  return fitAxis({ startMs, endMs }, dayPx, containerW);
}

/** THE dead-space fix: the canvas must be at least one scrollport wide. End-extends only - the
 * start never moves, so nothing the user is looking at shifts. containerW 0 (unmeasured static
 * render) is a no-op by construction. */
export function fitAxis(axis: Axis, dayPx: number, containerW: number): Axis {
  const needDays = Math.ceil(containerW / dayPx);
  const haveDays = dayIndex(axis.endMs, axis.startMs) + 1;
  if (haveDays >= needDays) return axis;
  return {
    startMs: axis.startMs,
    endMs: sundayOnOrAfter(axis.startMs + (needDays - 1) * DAY_MS),
  };
}

/** Extend one edge by up to `days`, clamped by the total cap. deltaDays 0 = at the cap - the
 * caller extends nothing and the user meets an honest hard edge. */
export function extendAxis(
  axis: Axis,
  edge: "start" | "end",
  days: number,
  capDays: number,
): { axis: Axis; deltaDays: number } {
  const total = dayIndex(axis.endMs, axis.startMs) + 1;
  const delta = Math.max(0, Math.min(days, capDays - total));
  if (delta === 0) return { axis, deltaDays: 0 };
  return edge === "start"
    ? { axis: { startMs: axis.startMs - delta * DAY_MS, endMs: axis.endMs }, deltaDays: delta }
    : { axis: { startMs: axis.startMs, endMs: axis.endMs + delta * DAY_MS }, deltaDays: delta };
}

/** Center-preserving zoom: the date under the viewport's midpoint stays put. Valid because a
 * zoom never moves axisStart (fit only extends the end). The upper clamp is the browser's. */
export function rezoomScrollLeft(
  scrollLeft: number,
  clientW: number,
  oldDayPx: number,
  newDayPx: number,
): number {
  return Math.max(0, (scrollLeft + clientW / 2) * (newDayPx / oldDayPx) - clientW / 2);
}

export type AxisModel = {
  top: MonthSpan[]; // band 1: months at fine zooms, years at coarse
  labels: Tick[]; // band 2: day numbers / week starts / month names
  gridlines: number[]; // hairline day-indices: Mondays (fine) or month starts (coarse)
  weekends: boolean; // shade weekends only where a day is wide enough to see
};

export function axisModel(zoom: Zoom, startMs: number, endMs: number): AxisModel {
  const fine = zoom === "week" || zoom === "month";
  const labels =
    zoom === "week"
      ? dayTicks(startMs, endMs)
      : zoom === "month"
        ? weekTicks(startMs, endMs)
        : monthTicks(startMs, endMs);
  const lines = fine ? weekTicks(startMs, endMs) : monthTicks(startMs, endMs);
  return {
    top: fine ? monthSpans(startMs, endMs) : yearSpans(startMs, endMs),
    labels,
    gridlines: lines.map((t) => dayIndex(t.ms, startMs)),
    weekends: fine,
  };
}
