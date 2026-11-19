/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import { DAY_MS } from "../core/date-math";
import {
  AXIS_CAP_DAYS,
  autoZoom,
  axisModel,
  extendAxis,
  fitAxis,
  initialAxis,
  rezoomScrollLeft,
  ZOOMS,
} from "./axis";

const D = (iso: string) => Date.parse(`${iso}T00:00:00Z`);
const days = (axis: { startMs: number; endMs: number }) => (axis.endMs - axis.startMs) / DAY_MS + 1;

describe("autoZoom", () => {
  it("picks the coarsest preset that still shows texture", () => {
    expect(autoZoom(30)).toBe("week");
    expect(autoZoom(42)).toBe("week");
    expect(autoZoom(43)).toBe("month");
    expect(autoZoom(120)).toBe("month");
    expect(autoZoom(365)).toBe("quarter");
    expect(autoZoom(366)).toBe("year");
  });
});

describe("initialAxis", () => {
  it("pads 3 days each side and snaps Monday…Sunday", () => {
    const axis = initialAxis(D("2026-07-15"), D("2026-07-20"), 24, 0);
    expect(new Date(axis.startMs).getUTCDay()).toBe(1);
    expect(new Date(axis.endMs).getUTCDay()).toBe(0);
    expect(axis.startMs).toBeLessThanOrEqual(D("2026-07-12"));
    expect(D("2026-07-12") - axis.startMs).toBeLessThan(7 * DAY_MS);
    expect(axis.endMs).toBeGreaterThanOrEqual(D("2026-07-23"));
  });
});

describe("fitAxis - the dead-space fix", () => {
  const short = { startMs: D("2026-07-06"), endMs: D("2026-07-19") }; // 14 days, Mon…Sun

  it("end-extends a short axis to fill the scrollport, Sunday-snapped, start unmoved", () => {
    const fitted = fitAxis(short, 24, 1200); // needs ceil(1200/24) = 50 days
    expect(fitted.startMs).toBe(short.startMs);
    expect(days(fitted)).toBeGreaterThanOrEqual(50);
    expect(new Date(fitted.endMs).getUTCDay()).toBe(0);
  });

  it("no-ops on a wide-enough axis and on an unmeasured container", () => {
    expect(fitAxis(short, 24, 0)).toBe(short);
    const wide = { startMs: D("2026-01-05"), endMs: D("2026-12-27") };
    expect(fitAxis(wide, 3, 800)).toBe(wide);
  });
});

describe("extendAxis", () => {
  const axis = { startMs: D("2026-07-06"), endMs: D("2026-07-19") };

  it("start-extends whole weeks and reports the compensation delta", () => {
    const { axis: out, deltaDays } = extendAxis(axis, "start", 42, AXIS_CAP_DAYS);
    expect(deltaDays).toBe(42);
    expect(out.startMs).toBe(axis.startMs - 42 * DAY_MS);
    expect(out.endMs).toBe(axis.endMs);
    expect(new Date(out.startMs).getUTCDay()).toBe(1); // 42 is whole weeks - the snap survives
  });

  it("end-extends without touching the start", () => {
    const { axis: out } = extendAxis(axis, "end", 42, AXIS_CAP_DAYS);
    expect(out.startMs).toBe(axis.startMs);
    expect(out.endMs).toBe(axis.endMs + 42 * DAY_MS);
  });

  it("clamps at the cap: a partial delta, then an honest zero", () => {
    const nearCap = {
      startMs: D("2026-01-05"),
      endMs: D("2026-01-05") + (AXIS_CAP_DAYS - 7) * DAY_MS,
    };
    const partial = extendAxis(nearCap, "start", 42, AXIS_CAP_DAYS);
    expect(partial.deltaDays).toBe(6);
    expect(days(partial.axis)).toBe(AXIS_CAP_DAYS);
    const capped = extendAxis(partial.axis, "start", 42, AXIS_CAP_DAYS);
    expect(capped.deltaDays).toBe(0);
    expect(capped.axis).toBe(partial.axis);
  });
});

describe("rezoomScrollLeft", () => {
  it("keeps the center date put through a zoom round trip", () => {
    const out = rezoomScrollLeft(4800, 1200, ZOOMS.week.dayPx, ZOOMS.quarter.dayPx);
    expect(rezoomScrollLeft(out, 1200, ZOOMS.quarter.dayPx, ZOOMS.week.dayPx)).toBeCloseTo(4800, 0);
  });

  it("clamps at zero near the left edge", () => {
    expect(rezoomScrollLeft(0, 1200, 24, 6)).toBe(0);
  });
});

describe("axisModel", () => {
  it("week zoom: month bands over day numbers, Monday gridlines, weekend shading on", () => {
    const m = axisModel("week", D("2026-07-06"), D("2026-08-30"));
    expect(m.top[0]?.label).toBe("Jul 2026");
    expect(m.labels[0]?.label).toBe("6");
    expect(m.labels).toHaveLength(56); // one per day
    expect(m.gridlines.slice(0, 3)).toEqual([0, 7, 14]);
    expect(m.weekends).toBe(true);
  });

  it("year zoom: year bands over month names, month-start gridlines, no weekend shading", () => {
    const m = axisModel("year", D("2026-11-02"), D("2027-03-28"));
    expect(m.top.map((s) => s.label)).toEqual(["2026", "2027"]);
    expect(m.labels.map((t) => t.label)).toEqual(["Dec", "Jan 2027", "Feb", "Mar"]);
    expect(m.weekends).toBe(false);
  });
});
