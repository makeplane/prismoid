/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import { collapseRuns, decisionLabel, type RunRow } from "./streaks";

const run = (decision: string, i: number): RunRow =>
  ({
    id: `r${i}`,
    occurrence: `o${i}`,
    source: "clock",
    decision,
    task_id: null,
    task_status: null,
    scheduled_for: null,
    at: new Date(2026, 7, 25, 12, i).toISOString(),
    detail: "",
  }) as RunRow;

describe("collapseRuns", () => {
  it("folds consecutive quiet rows and keeps news standalone", () => {
    const rows = [
      run("minted", 9),
      run("skipped_overlap", 8),
      run("skipped_overlap", 7),
      run("skipped_missed", 6),
      run("error", 5), // an error is news, never quiet
      run("skipped_cap", 4),
      run("held", 3),
    ];
    const items = collapseRuns(rows);
    expect(items.map((i) => i.kind)).toEqual(["run", "streak", "run", "streak", "run"]);
    const streak = items[1];
    if (streak.kind !== "streak") throw new Error("expected streak");
    expect(streak.count).toBe(3);
    expect(streak.decisions).toEqual(["skipped_overlap", "skipped_missed"]);
  });

  it("a fire the allowance skipped is quiet: a spent workspace does not flood the ledger", () => {
    const items = collapseRuns([
      run("skipped_allowance", 3),
      run("skipped_allowance", 2),
      run("minted", 1),
    ]);
    expect(items.map((i) => i.kind)).toEqual(["streak", "run"]);
  });

  it("suppressed_loop is loud - the visible Exchange drop never collapses", () => {
    const items = collapseRuns([run("suppressed_loop", 2), run("skipped_overlap", 1)]);
    expect(items[0]).toMatchObject({ kind: "run" });
  });
});

describe("decisionLabel", () => {
  it("spaces a stored decision", () => {
    expect(decisionLabel("skipped_overlap")).toBe("skipped overlap");
    expect(decisionLabel("minted")).toBe("minted");
    expect(decisionLabel("skipped_allowance")).toBe("skipped allowance");
  });
});
