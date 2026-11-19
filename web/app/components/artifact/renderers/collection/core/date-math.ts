/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** Pure UTC calendar math for the gantt and calendar lenses. Everything works in UTC-midnight
 * milliseconds so a bar's width never changes across a DST boundary or a viewer's timezone -
 * collection dates are calendar dates, not instants. No date library (~40 lines of
 * arithmetic do not earn a dependency). */

export const DAY_MS = 86_400_000;

export function utcDay(ms: number): number {
  return Math.floor(ms / DAY_MS);
}

/** Whole days from `minMs` to `ms` (both UTC midnights). */
export function dayIndex(ms: number, minMs: number): number {
  return utcDay(ms) - utcDay(minMs);
}

/** UTC-midnight ms → the document's "YYYY-MM-DD". The ONE ms→ISO writer (drag commits). */
export function isoDate(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

/** Inclusive span in days: a bar that starts and ends the same day is 1 day wide. */
export function span(startMs: number, endMs: number): number {
  return Math.max(1, dayIndex(endMs, startMs) + 1);
}

export type Tick = { ms: number; label: string };

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Axis ticks for a [min, max] range: week ticks for short ranges, month ticks beyond. */
export function ticksFor(minMs: number, maxMs: number): { unit: "week" | "month"; ticks: Tick[] } {
  const days = dayIndex(maxMs, minMs) + 1;
  if (days <= 60) {
    const ticks: Tick[] = [];
    // First Monday at or after min, then every 7 days.
    let ms = minMs;
    while (new Date(ms).getUTCDay() !== 1 && ms <= maxMs) ms += DAY_MS;
    for (; ms <= maxMs; ms += 7 * DAY_MS) {
      const d = new Date(ms);
      ticks.push({ ms, label: `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}` });
    }
    return { unit: "week", ticks };
  }
  const ticks: Tick[] = [];
  const first = new Date(minMs);
  let year = first.getUTCFullYear();
  let month = first.getUTCMonth();
  for (;;) {
    month += 1;
    if (month > 11) {
      month = 0;
      year += 1;
    }
    const ms = Date.UTC(year, month, 1);
    if (ms > maxMs) break;
    ticks.push({ ms, label: `${MONTHS[month]}${month === 0 ? ` ${year}` : ""}` });
  }
  return { unit: "month", ticks };
}

export type MonthSpan = { label: string; startDay: number; days: number };

/** Month bands for the gantt's top header row: each month's [start-day-index, width-in-days]
 * within [minMs, maxMs]. Partial first/last months are clipped honestly. */
export function monthSpans(minMs: number, maxMs: number): MonthSpan[] {
  const spans: MonthSpan[] = [];
  const first = new Date(minMs);
  let year = first.getUTCFullYear();
  let month = first.getUTCMonth();
  for (;;) {
    const start = Math.max(Date.UTC(year, month, 1), minMs);
    if (start > maxMs) break;
    const end = Math.min(Date.UTC(year, month + 1, 1) - DAY_MS, maxMs);
    spans.push({
      label: `${MONTHS[month]} ${year}`,
      startDay: dayIndex(start, minMs),
      days: dayIndex(end, start) + 1,
    });
    month += 1;
    if (month > 11) {
      month = 0;
      year += 1;
    }
  }
  return spans;
}

export function isWeekend(ms: number): boolean {
  const dow = new Date(ms).getUTCDay();
  return dow === 0 || dow === 6;
}

/** The Monday at or before a UTC midnight. Idempotent on Mondays. */
export function mondayOnOrBefore(ms: number): number {
  const dow = (new Date(ms).getUTCDay() + 6) % 7; // Monday = 0
  return ms - dow * DAY_MS;
}

/** The Sunday at or after a UTC midnight. Idempotent on Sundays. */
export function sundayOnOrAfter(ms: number): number {
  const dow = new Date(ms).getUTCDay(); // Sunday = 0
  return dow === 0 ? ms : ms + (7 - dow) * DAY_MS;
}

/** One tick per day labeled with the UTC day-of-month - the week zoom's bottom band. */
export function dayTicks(minMs: number, maxMs: number): Tick[] {
  const ticks: Tick[] = [];
  for (let ms = minMs; ms <= maxMs; ms += DAY_MS) {
    ticks.push({ ms, label: String(new Date(ms).getUTCDate()) });
  }
  return ticks;
}

/** One tick per Monday - the month zoom's bottom band. */
export function weekTicks(minMs: number, maxMs: number): Tick[] {
  const ticks: Tick[] = [];
  let ms = mondayOnOrBefore(minMs);
  if (ms < minMs) ms += 7 * DAY_MS;
  for (; ms <= maxMs; ms += 7 * DAY_MS) {
    const d = new Date(ms);
    ticks.push({ ms, label: `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}` });
  }
  return ticks;
}

/** One tick per month start within the range; January carries its year so a long axis stays
 * self-locating - the quarter/year zooms' bottom band. */
export function monthTicks(minMs: number, maxMs: number): Tick[] {
  const ticks: Tick[] = [];
  const first = new Date(minMs);
  let year = first.getUTCFullYear();
  let month = first.getUTCMonth();
  if (Date.UTC(year, month, 1) < minMs) {
    month += 1;
    if (month > 11) {
      month = 0;
      year += 1;
    }
  }
  for (;;) {
    const ms = Date.UTC(year, month, 1);
    if (ms > maxMs) break;
    ticks.push({ ms, label: `${MONTHS[month]}${month === 0 ? ` ${year}` : ""}` });
    month += 1;
    if (month > 11) {
      month = 0;
      year += 1;
    }
  }
  return ticks;
}

/** Year bands for the gantt's top header at coarse zooms - like monthSpans, partials clipped. */
export function yearSpans(minMs: number, maxMs: number): MonthSpan[] {
  const spans: MonthSpan[] = [];
  let year = new Date(minMs).getUTCFullYear();
  for (;;) {
    const start = Math.max(Date.UTC(year, 0, 1), minMs);
    if (start > maxMs) break;
    const end = Math.min(Date.UTC(year + 1, 0, 1) - DAY_MS, maxMs);
    spans.push({
      label: String(year),
      startDay: dayIndex(start, minMs),
      days: dayIndex(end, start) + 1,
    });
    year += 1;
  }
  return spans;
}

export function shortDate(ms: number): string {
  const d = new Date(ms);
  return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
}

export type MonthCell = { ms: number; inMonth: boolean; day: number };

/** A 6×7 UTC month grid starting on Monday - always 42 cells, leading/trailing spill marked. */
export function monthGrid(year: number, month: number): MonthCell[] {
  const first = Date.UTC(year, month, 1);
  const firstDow = (new Date(first).getUTCDay() + 6) % 7; // Monday = 0
  const start = first - firstDow * DAY_MS;
  const cells: MonthCell[] = [];
  for (let i = 0; i < 42; i++) {
    const ms = start + i * DAY_MS;
    const d = new Date(ms);
    cells.push({ ms, inMonth: d.getUTCMonth() === month, day: d.getUTCDate() });
  }
  return cells;
}

export function monthLabel(year: number, month: number): string {
  return `${["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"][month]} ${year}`;
}
