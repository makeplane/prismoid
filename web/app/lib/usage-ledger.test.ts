/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The ledger's vocabulary, pinned: tokens that explain the cost, a search that is not a model
// call, and a breakdown a person can read (the model's name, the step's title).

import { describe, expect, it } from "vitest";
import type { FeedItem } from "./task-view-types";
import {
  EMPTY_LEDGER,
  callsLabel,
  isModelCall,
  isSearch,
  recordInLedger,
  splitTokens,
  usageBreakdown,
  withModel,
} from "./usage-ledger";

describe("splitTokens", () => {
  it("keeps the cache's tokens apart from the prompt and reply", () => {
    expect(
      splitTokens({
        input_token: 10,
        output_token: 12,
        cache_write_token: 12_800,
        cache_read_token: 35_000,
      }),
    ).toEqual({ tokens: 22, cached: 47_800 });
    expect(splitTokens(undefined)).toEqual({ tokens: 0, cached: 0 });
  });
});

describe("callsLabel", () => {
  it("names model calls and searches apart, singular and plural", () => {
    expect(callsLabel(8, 2)).toBe("8 model calls · 2 searches");
    expect(callsLabel(1, 0)).toBe("1 model call");
    expect(callsLabel(0, 1)).toBe("1 search");
    expect(callsLabel(0, 0)).toBe("0 model calls");
  });
});

describe("withModel", () => {
  it("lists each model once, in first-use order", () => {
    expect(withModel(withModel(withModel(undefined, "sonnet"), "haiku"), "sonnet")).toEqual([
      "sonnet",
      "haiku",
    ]);
  });
});

describe("the breakdown", () => {
  const execution = {
    kind: "execution",
    groups: [{ id: "s1", kind: "step", title: "Write the memo" }],
  } as unknown as FeedItem;

  it("is by model and by step title, with what no step owns in its own row", () => {
    let ledger = EMPTY_LEDGER;
    const rows: Parameters<typeof recordInLedger>[1][] = [
      { model: "claude-sonnet-5-5", stepId: "s1", usd: 0.08, units: { input_token: 5 } },
      { model: "claude-haiku-5-5", stepId: "s1", usd: 0.01, units: { input_token: 5 } },
      { model: "brave", stepId: "s1", usd: 0.005, units: { search: 1 } },
      { model: "claude-haiku-5-5", stepId: null, usd: 0.002, units: { input_token: 5 } },
    ];
    for (const row of rows) ledger = recordInLedger(ledger, row);
    expect(isSearch({ search: 1 })).toBe(true);

    const out = usageBreakdown({ usageLedger: ledger, usageUsd: 0.097, feed: [execution] });
    expect(out.models.map((m) => m.model)).toEqual([
      "claude-sonnet-5-5",
      "claude-haiku-5-5",
      "brave",
    ]);
    expect(out.models.find((m) => m.model === "claude-haiku-5-5")).toMatchObject({ calls: 2 });
    expect(out.models.find((m) => m.model === "brave")).toMatchObject({ calls: 0, searches: 1 });
    expect(out.steps.map((s) => s.title)).toEqual(["Write the memo", "Not tied to a step"]);
    expect(out.units).toContainEqual(["search", 1]);
  });

  it("counts Machine time as its own row, never as a model call", () => {
    let ledger = EMPTY_LEDGER;
    ledger = recordInLedger(ledger, {
      model: "claude-sonnet-5-5",
      stepId: "s1",
      usd: 0.05,
      units: { input_token: 5 },
    });
    ledger = recordInLedger(ledger, {
      model: "machine.standard",
      stepId: null,
      usd: 0.02,
      units: { second: 600 },
    });
    expect(isModelCall({ second: 600 })).toBe(false);
    expect(ledger.byModel["machine.standard"]).toMatchObject({
      calls: 0,
      searches: 0,
      seconds: 600,
    });
    expect(ledger.byModel["claude-sonnet-5-5"]).toMatchObject({ calls: 1, seconds: 0 });
  });
});
