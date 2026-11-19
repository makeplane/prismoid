/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// What the task stream does with ONE frame, pure (hooks/use-task-stream.ts wires it to the
// socket). The load-bearing rule: presence is state about who is HERE - it folds
// into presence, never into the thread, and never counts as the task being alive.

import { afterEach, describe, expect, it, vi } from "vitest";
import { emptyPresence, presentPeople } from "./presence";
import { foldFrame, foldKept, type FrameState } from "./stream-frame";
import { STALL_MS } from "./stream-stall";
import { initialView } from "./task-view";

type Detail = Parameters<typeof initialView>[0];

const start = (): FrameState => ({
  view: initialView({
    status: "running",
    goal: "g",
    cost: { total_usd: "0" },
    plan: { steps: [] },
  } as unknown as Detail),
  presence: emptyPresence(),
  lastSignalAt: 1_000,
});

const ADA = { type: "user", id: "ada-id", name: "Ada" };
const presence = (type: string, over: Record<string, unknown> = {}) =>
  JSON.stringify({ type, task_id: "t", conn: "conn-ada-1", actor: ADA, editor: true, ...over });

afterEach(() => vi.restoreAllMocks());

describe("foldFrame", () => {
  it("a presence frame lands in the presence and touches nothing else - not the view, not the clock", () => {
    const s = start();
    const r = foldFrame(s, "presence.join", presence("presence.join"), 60_000);
    expect(presentPeople(r.presence, 60_000).map((p) => p.id)).toEqual(["ada-id"]);
    expect(r.view).toBe(s.view); // the very same object: the reducer never saw it
    expect(r.lastSignalAt).toBe(s.lastSignalAt); // the watchdog's clock did not move
    expect(r.alive).toBe(false);
    expect(r.changed).toBe("presence");
  });

  it("a presence that only ever hears presence still goes stale: the clock is the task's", () => {
    let s = start();
    for (let t = 2_000; t <= 1_000 + STALL_MS + 5_000; t += 5_000)
      s = { ...s, ...foldFrame(s, "presence.here", presence("presence.here"), t) };
    expect(s.lastSignalAt).toBe(1_000);
  });

  it("a task frame folds into the view and proves the task alive", () => {
    const s = start();
    const r = foldFrame(
      s,
      "task.status",
      JSON.stringify({ type: "task.status", seq: 1, status: "paused" }),
      9_000,
    );
    expect(r.view.status).toBe("paused");
    expect(r.lastSignalAt).toBe(9_000);
    expect(r.alive).toBe(true);
    expect(r.changed).toBe("view");
    expect(r.presence).toBe(s.presence);
  });

  it("a frame named presence but shaped like a task event is dropped, never reduced", () => {
    const s = start();
    const r = foldFrame(
      s,
      "presence.here",
      JSON.stringify({ type: "task.status", seq: 1, status: "cancelled" }),
      9_000,
    );
    expect(r.view).toBe(s.view);
    expect(r.view.status).toBe("running");
    expect(r.changed).toBeNull();
    expect(r.lastSignalAt).toBe(s.lastSignalAt);
  });

  it("and the mirror: a task-named frame carrying presence is dropped from the presence too", () => {
    const s = start();
    const r = foldFrame(s, "task.status", presence("presence.join"), 9_000);
    expect(r.presence).toBe(s.presence);
    expect(r.view).toBe(s.view);
    expect(r.changed).toBeNull();
  });

  it("a malformed frame is ignored, never fatal", () => {
    const s = start();
    const r = foldFrame(s, "task.status", "{not json", 9_000);
    expect(r.view).toBe(s.view);
    expect(r.changed).toBeNull();
  });

  it("one unfoldable frame is skipped and the stream keeps folding", () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const s = start();
    const bad = foldFrame(s, "task.status", JSON.stringify({ type: "no.such.event" }), 9_000);
    expect(bad.view).toBe(s.view);
    expect(bad.changed).toBeNull();
    const good = foldFrame(
      { ...s, ...bad },
      "task.status",
      JSON.stringify({ type: "task.status", seq: 2, status: "paused" }),
      10_000,
    );
    expect(good.view.status).toBe("paused");
  });

  it("a presence frame that cannot be folded is dropped, never thrown at the socket", () => {
    const s = start();
    // no `actor`: foldPresence reads frame.actor.id
    const r = foldFrame(
      s,
      "presence.here",
      JSON.stringify({ type: "presence.here", task_id: "t", conn: "conn-1" }),
      9_000,
    );
    expect(r.presence).toBe(s.presence);
    expect(r.view).toBe(s.view);
    expect(r.changed).toBeNull();
  });
});

describe("foldKept", () => {
  it("folding the frames a tab kept gives the view the server's replay of them gives", () => {
    const frames = [
      {
        type: "step.state",
        data: JSON.stringify({
          type: "step.state",
          seq: 1,
          step_id: "s1",
          title: "Write",
          state: "running",
          attempt: 1,
          agent: "writer",
          detail: "",
        }),
      },
      {
        type: "step.narration",
        data: JSON.stringify({ type: "step.narration", seq: 2, markdown: "Here is the outline." }),
      },
      {
        type: "task.status",
        data: JSON.stringify({ type: "task.status", seq: 3, status: "paused" }),
      },
    ];
    let replayed = start();
    for (const f of frames)
      replayed = { ...replayed, ...foldFrame(replayed, f.type, f.data, 5_000) };
    const resumed = foldKept(start().view, frames);
    expect(resumed).toEqual(replayed.view);
    expect(resumed.appliedSeq).toBe(3);
    expect(resumed.status).toBe("paused");
  });
});
