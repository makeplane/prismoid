/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The one time-scale law, pinned where it lives. A duration anywhere in the app - the
// ticker, the tree's clocks and settled facts, gate countdowns - speaks these two
// functions, so a scale change is a deliberate act here, never a drive-by elsewhere.
import { describe, expect, it } from "vitest";
import { formatDurationMs, formatSeconds, formatTokens } from "./time";

describe("formatSeconds", () => {
  it("speaks human time scales - seconds, minutes, hours, then DAYS at the ceiling (user-locked 2026-08-08)", () => {
    expect(formatSeconds(0)).toBe("0s");
    expect(formatSeconds(1.9)).toBe("1s");
    expect(formatSeconds(45)).toBe("45s");
    expect(formatSeconds(65)).toBe("1m 5s");
    expect(formatSeconds(90)).toBe("1m 30s");
    expect(formatSeconds(3661)).toBe("1h 1m 1s");
    expect(formatSeconds(7275)).toBe("2h 1m 15s");
    expect(formatSeconds(86_405)).toBe("1d 0h 0m 5s");
    expect(formatSeconds(90_061)).toBe("1d 1h 1m 1s");
    expect(formatSeconds(30 * 86_400)).toBe("30d 0h 0m 0s"); // days, never weeks or months
  });

  it("never goes negative or NaN", () => {
    expect(formatSeconds(-5)).toBe("0s");
    expect(formatSeconds(Number.NaN)).toBe("0s");
  });
});

describe("formatDurationMs", () => {
  it("keeps one decimal under ten seconds, then the human scales up through hours", () => {
    expect(formatDurationMs(3_420)).toBe("3.4s");
    expect(formatDurationMs(41_000)).toBe("41s");
    expect(formatDurationMs(134_000)).toBe("2m 14s");
    expect(formatDurationMs(7_275_000)).toBe("2h 1m 15s");
  });
});

describe("formatTokens", () => {
  it("spells one figure, with the cached tokens that carry the cost beside it", () => {
    expect(formatTokens(22)).toBe("22 tok");
    expect(formatTokens(3400)).toBe("3.4k tok");
    expect(formatTokens(22, 12_800)).toBe("22 tok + 12.8k cached");
    expect(formatTokens(0, 840)).toBe("840 cached");
  });
});
