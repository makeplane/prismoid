/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The routed record when the planner fails: the one-step floor ran, and the receipt says so on
// its line, not only in a log the person never sees.

import { describe, expect, it } from "vitest";
import type { TaskEvent } from "~/lib/api/events";
import { initialView, reduce, routeLabel, type TaskView } from "./task-view";

const at = "2026-11-06T00:00:00Z";

function planned(extra: Partial<Extract<TaskEvent, { type: "task.planned" }>>): TaskEvent {
  return {
    type: "task.planned",
    seq: 2,
    task_id: "t1",
    lane: "record",
    at,
    plan: { steps: [] },
    route: "standard",
    route_source: "planner",
    ...extra,
  } as TaskEvent;
}

function view(): TaskView {
  return initialView({
    status: "planning",
    goal: "a memo",
    cost: { total_usd: "0" },
    plan: { steps: [] },
  } as never);
}

describe("a planner that failed", () => {
  it("wears its failure on the route and the receipt line names it", () => {
    const v = reduce(
      view(),
      planned({
        route_why: "the planner failed, so one step does the work: ProviderError: overloaded",
        fallback: "ProviderError: overloaded",
      }),
    );
    expect(v.route?.fallback).toBe("ProviderError: overloaded");
    expect(routeLabel(v.route!)).toBe("planner failed, one step");
  });

  it("a planned turn keeps its plain label and carries no fallback", () => {
    const v = reduce(view(), planned({ route_why: "One proposed the plan" }));
    expect(v.route).toEqual({ lane: "standard", source: "planner", why: "One proposed the plan" });
    expect(routeLabel(v.route!)).toBe("planned");
  });
});
