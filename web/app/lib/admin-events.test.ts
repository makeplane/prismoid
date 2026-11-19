/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The admin peek's fold-from-fetched-events adapter: raw wire payload objects (identical to
// the owner's stream frames) must fold through the same reducer as replay, junk must be
// dropped rather than wedge the fold, and the incremental poll cursor must be the highest
// durable seq seen.

import { describe, expect, it } from "vitest";
import { foldAdminEvents, maxSeq, toTaskEvents } from "./admin-events";

const record: unknown[] = [
  { type: "task.created", seq: 1, at: "2026-08-23T00:00:00Z", task_id: "t1", goal: "write a memo" },
  { type: "task.status", seq: 2, at: "2026-08-23T00:00:01Z", task_id: "t1", status: "planning" },
  {
    type: "task.status",
    seq: 3,
    at: "2026-08-23T00:00:02Z",
    task_id: "t1",
    status: "running",
  },
  {
    type: "usage.recorded",
    seq: 4,
    at: "2026-08-23T00:00:03Z",
    task_id: "t1",
    cost_usd: "0.0100",
    model: "m",
    family: "f",
  },
];

describe("toTaskEvents", () => {
  it("keeps event-shaped payloads and drops junk", () => {
    const raw = [
      ...record,
      null,
      "not an event",
      42,
      { seq: 9 }, // no type
      { type: 7 }, // non-string type
    ];
    expect(toTaskEvents(raw)).toHaveLength(record.length);
  });
});

describe("foldAdminEvents", () => {
  it("folds raw payloads through the replay reducer", () => {
    const v = foldAdminEvents(record);
    expect(v.status).toBe("running");
    expect(v.goal).toBe("write a memo");
    expect(v.usageCalls).toBe(1);
    expect(v.appliedSeq).toBe(4);
  });

  it("tolerates deploy skew - an unknown event type is skipped, not fatal", () => {
    const v = foldAdminEvents([
      ...record,
      { type: "task.some_future_thing", seq: 5, task_id: "t1" },
    ]);
    expect(v.status).toBe("running");
  });

  it("never double-applies a replayed durable frame (seq dedup survives the adapter)", () => {
    const v = foldAdminEvents([...record, ...record]);
    expect(v.usageCalls).toBe(1);
  });
});

describe("maxSeq", () => {
  it("returns the highest durable seq in a batch", () => {
    expect(maxSeq(record)).toBe(4);
  });

  it("is 0 for an empty or seq-less batch", () => {
    expect(maxSeq([])).toBe(0);
    expect(maxSeq([{ type: "activity" }, null])).toBe(0);
  });
});
