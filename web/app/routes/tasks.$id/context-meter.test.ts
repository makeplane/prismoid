/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";

import { contextCeiling, laneBudget } from "./context-meter";

const lane = (name: "instructions" | "thread", tokens: number, budget?: number | null) => ({
  lane: name,
  tokens,
  truncated: false,
  budget,
  items: [],
});

describe("context budgets come from the server", () => {
  it("sums the ceilings the server stated for the lanes it sent", () => {
    const context = {
      totalTokens: 900,
      lanes: [lane("instructions", 400, 1000), lane("thread", 500, 12000)],
    };
    expect(contextCeiling(context)).toBe(13000);
  });

  it("states no ceiling when nothing reached the task", () => {
    expect(contextCeiling(null)).toBe(0);
    expect(contextCeiling({ totalTokens: 0, lanes: [] })).toBe(0);
  });

  it("leaves an unbudgeted or older lane without a ceiling", () => {
    expect(laneBudget(lane("thread", 10))).toBeNull();
    expect(laneBudget(lane("thread", 10, null))).toBeNull();
    expect(contextCeiling({ totalTokens: 10, lanes: [lane("thread", 10)] })).toBe(0);
  });
});
