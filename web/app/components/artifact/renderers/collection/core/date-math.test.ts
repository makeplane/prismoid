/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import {
  DAY_MS,
  dayIndex,
  dayTicks,
  isoDate,
  mondayOnOrBefore,
  monthGrid,
  monthTicks,
  span,
  sundayOnOrAfter,
  ticksFor,
  utcDay,
  weekTicks,
  yearSpans,
} from "./date-math";

const D = (iso: string) => Date.parse(`${iso}T00:00:00Z`);

describe("day arithmetic (UTC - immune to DST and viewer timezones)", () => {
  it("indexes days across a DST boundary without drift", () => {
    // US DST springs forward 2026-03-08; UTC math must not care.
    expect(dayIndex(D("2026-03-10"), D("2026-03-06"))).toBe(4);
  });

  it("a same-day bar is one day wide", () => {
    expect(span(D("2026-08-01"), D("2026-08-01"))).toBe(1);
    expect(span(D("2026-08-01"), D("2026-08-03"))).toBe(3);
  });

  it("utcDay is stable through the day", () => {
    expect(utcDay(D("2026-08-01") + DAY_MS - 1)).toBe(utcDay(D("2026-08-01")));
  });

  it("isoDate is the inverse of cell date parsing and shifts across month boundaries", () => {
    expect(isoDate(D("2026-07-31"))).toBe("2026-07-31");
    expect(isoDate(D("2026-07-31") + 3 * DAY_MS)).toBe("2026-08-03");
    expect(isoDate(D("2026-01-01") - DAY_MS)).toBe("2025-12-31");
  });
});

describe("ticksFor", () => {
  it("short ranges get week ticks, long ranges month ticks", () => {
    expect(ticksFor(D("2026-07-01"), D("2026-07-28")).unit).toBe("week");
    expect(ticksFor(D("2026-01-01"), D("2026-08-01")).unit).toBe("month");
  });

  it("month ticks land on the first of each month inside the range", () => {
    const { ticks } = ticksFor(D("2026-01-15"), D("2026-04-10"));
    expect(ticks.map((t) => t.label)).toEqual(["Feb", "Mar", "Apr"]);
  });
});

describe("monthGrid", () => {
  it("is always 42 Monday-first cells with spill marked", () => {
    const cells = monthGrid(2026, 7); // August 2026 - starts on a Saturday
    expect(cells).toHaveLength(42);
    expect(new Date(cells[0].ms).getUTCDay()).toBe(1); // Monday
    expect(cells.filter((c) => c.inMonth)).toHaveLength(31);
    expect(cells[0].inMonth).toBe(false);
  });
});

describe("axis primitives (gantt v3)", () => {
  it("mondayOnOrBefore / sundayOnOrAfter snap and are idempotent", () => {
    expect(mondayOnOrBefore(D("2026-08-05"))).toBe(D("2026-08-03")); // Wednesday → its Monday
    expect(mondayOnOrBefore(D("2026-08-03"))).toBe(D("2026-08-03")); // already Monday
    expect(mondayOnOrBefore(D("2026-08-02"))).toBe(D("2026-07-27")); // Sunday belongs to LAST week
    expect(sundayOnOrAfter(D("2026-08-05"))).toBe(D("2026-08-09"));
    expect(sundayOnOrAfter(D("2026-08-09"))).toBe(D("2026-08-09"));
  });

  it("dayTicks crosses a DST boundary with consecutive day numbers", () => {
    const ticks = dayTicks(D("2026-03-07"), D("2026-03-10"));
    expect(ticks.map((t) => t.label)).toEqual(["7", "8", "9", "10"]);
  });

  it("weekTicks are all Mondays, including a range-start Monday", () => {
    const ticks = weekTicks(D("2026-07-01"), D("2026-07-31"));
    expect(ticks.map((t) => t.label)).toEqual(["Jul 6", "Jul 13", "Jul 20", "Jul 27"]);
    for (const t of ticks) expect(new Date(t.ms).getUTCDay()).toBe(1);
    expect(weekTicks(D("2026-08-03"), D("2026-08-10"))[0]?.label).toBe("Aug 3");
  });

  it("monthTicks start at the first month boundary; January carries its year", () => {
    const long = monthTicks(D("2026-11-15"), D("2027-02-10"));
    expect(long.map((t) => t.label)).toEqual(["Dec", "Jan 2027", "Feb"]);
    const exact = monthTicks(D("2026-07-01"), D("2026-08-31"));
    expect(exact.map((t) => t.label)).toEqual(["Jul", "Aug"]); // a range-start month counts
  });

  it("yearSpans clip partial years and account for every day", () => {
    const spans = yearSpans(D("2026-11-01"), D("2027-01-31"));
    expect(spans.map((s) => s.label)).toEqual(["2026", "2027"]);
    expect(spans[0]).toMatchObject({ startDay: 0, days: 61 }); // Nov + Dec, clipped
    expect(spans[1]).toMatchObject({ startDay: 61, days: 31 });
  });
});
