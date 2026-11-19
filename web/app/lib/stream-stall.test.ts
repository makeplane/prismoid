/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import { heard, isStalled, provesLife, STALL_MS, systemWaitLimitMs } from "./stream-stall";
import type { FeedItem } from "~/lib/task-view";

const PRESENCE = ["presence.join", "presence.here", "presence.leave", "presence.typing"];

describe("the stall watchdog", () => {
  it("a stream that hears only presence past STALL_MS reports stalled", () => {
    // Every open stream hears its own `here` each reauth wake (10 s) - the task has viewers,
    // the work is not moving.
    let last = 0;
    for (let t = 1_000; t <= STALL_MS + 5_000; t += 1_000)
      last = heard(last, PRESENCE[t % PRESENCE.length], t);
    expect(last).toBe(0);
    expect(isStalled("running", last, STALL_MS + 5_000)).toBe(true);
  });

  it("a task frame clears it", () => {
    const now = STALL_MS + 5_000;
    const last = heard(0, "progress", now);
    expect(last).toBe(now);
    expect(isStalled("running", last, now + 1_000)).toBe(false);
  });

  it("presence never proves life; every task frame does", () => {
    for (const type of PRESENCE) expect(provesLife(type)).toBe(false);
    for (const type of ["progress", "message.delta", "activity", "task.status"])
      expect(provesLife(type)).toBe(true);
  });

  it("screen.* frames (the live screen's state and action) are not the task's signal either", () => {
    for (const type of ["screen.state", "screen.action"]) {
      expect(provesLife(type)).toBe(false);
      expect(heard(5, type, 99)).toBe(5);
    }
    expect(provesLife("machine.control_ended")).toBe(true);
  });

  it("a settled task never stalls", () => {
    expect(isStalled("delivered", 0, STALL_MS * 10)).toBe(false);
  });
});

describe("isStalled with a longer bound", () => {
  it("a wait with a stated bound is quiet past the normal window and stalled past its own", () => {
    const last = 1_000;
    const bound = 10 * STALL_MS;
    expect(isStalled("running", last, last + STALL_MS + 1, bound)).toBe(false);
    expect(isStalled("running", last, last + bound + 1, bound)).toBe(true);
    expect(isStalled("delivered", last, last + bound + 1, bound)).toBe(false);
  });
});

describe("systemWaitLimitMs", () => {
  const feed = (rows: unknown[], settled = false) =>
    [
      { kind: "execution", id: "x", settled, groups: [{ id: "s1", kind: "step", rows }] },
    ] as unknown as FeedItem[];
  const machine = { t: "tool", id: "m", tool: "machine", pending: true };

  it("is the bound the server stated on the pending Machine row, in ms", () => {
    expect(systemWaitLimitMs(feed([{ ...machine, waitS: 600 }]))).toBe(600_000);
  });

  it("is null without a stated bound (an older event), without a pending Machine row, or settled", () => {
    expect(systemWaitLimitMs(feed([machine]))).toBeNull();
    expect(systemWaitLimitMs(feed([{ ...machine, waitS: 600, pending: false }]))).toBeNull();
    expect(systemWaitLimitMs(feed([{ ...machine, waitS: 600 }], true))).toBeNull();
    expect(systemWaitLimitMs([])).toBeNull();
  });
});
