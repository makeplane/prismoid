/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The reducer is the client's half of the streaming contract: fold durable frames
// exactly once, let ephemeral deltas flow, and never lose the reader's place. These tests pin the
// properties the UI silently depends on - seq dedup (usage counts, critically), the render-skip
// identity on replayed frames, and the m10.2 rule that a narration closing a live segment with
// the same text must not produce a second bubble.

import { describe, expect, it } from "vitest";
import type { TaskEvent } from "~/lib/api/events";
import {
  addOptimisticUser,
  deliveredArtifacts,
  dockFor,
  gatesFor,
  initialView,
  reduce,
  removeOptimistic,
  reseedScreen,
  steerWhileWaiting,
  type ExecRow,
  type TaskView,
} from "./task-view";

type Detail = Parameters<typeof initialView>[0];

const detail = (over: Record<string, unknown> = {}): Detail =>
  ({
    status: "created",
    goal: "write a memo",
    cost: { total_usd: "0.0400" },
    plan: { steps: [] },
    ...over,
  }) as unknown as Detail;

// Wire-shaped partials: the reducer only reads `type`, `seq`, and per-type fields, and the tests
// say exactly what is on the wire - a full envelope would be noise.
const ev = (frame: Record<string, unknown>): TaskEvent => frame as unknown as TaskEvent;

const fold = (view: TaskView, ...frames: TaskEvent[]): TaskView => frames.reduce(reduce, view);

/** The live turn's execution item - the tree most assertions dig into. */
const exec = (v: TaskView) => {
  const item = v.feed.find((i) => i.kind === "execution");
  return item?.kind === "execution" ? item : undefined;
};

/** The canonical "a step is running" frame - the tree tests' most-typed fixture. */
const stepRunning = (seq: number, over: Record<string, unknown> = {}): TaskEvent =>
  ev({
    type: "step.state",
    seq,
    step_id: "s1",
    title: "Write",
    state: "running",
    attempt: 1,
    agent: "writer",
    detail: "",
    ...over,
  });

describe("the usage ledger - tokens that explain the cost, every model, searches apart", () => {
  const usage = (seq: number, model: string, units: Record<string, number>, cost = "0.01") =>
    ev({ type: "usage.recorded", seq, step_id: "s1", model, cost_usd: cost, units });

  it("a step folds cached tokens beside fresh ones, names every model, and counts searches apart", () => {
    const v = fold(
      initialView(detail()),
      stepRunning(1),
      usage(2, "claude-sonnet-5-5", {
        input_token: 10,
        output_token: 12,
        cache_write_token: 12_800,
      }),
      usage(3, "claude-haiku-5-5", { input_token: 4, cache_read_token: 900 }),
      usage(4, "brave", { search: 1 }),
      usage(5, "claude-sonnet-5-5", { input_token: 1, output_token: 1 }),
    );
    const g = exec(v)?.groups.find((x) => x.id === "s1");
    expect(g).toMatchObject({
      calls: 3,
      searches: 1,
      tokens: 28,
      cachedTokens: 13_700,
      models: ["claude-sonnet-5-5", "claude-haiku-5-5"], // the search engine is not a model
    });
    expect(v.usageCalls).toBe(3);
    expect(v.usageSearches).toBe(1);
    expect(v.usageLedger.byModel["claude-haiku-5-5"]).toMatchObject({ calls: 1, usd: 0.01 });
  });

  it("the Delivered receipt carries the turn's cached tokens", () => {
    const v = fold(
      initialView(detail()),
      stepRunning(1),
      usage(2, "m", { input_token: 22, cache_write_token: 12_800 }),
      stepRunning(3, { state: "done" }),
      ev({ type: "task.delivered", seq: 4 }),
    );
    const ep = v.feed.find((i) => i.kind === "epilogue");
    expect(ep?.kind === "epilogue" ? ep.receipt : undefined).toMatchObject({
      tokens: 22,
      cachedTokens: 12_800,
    });
  });
});

describe("the phase law (round six) - work and output interleave chronologically", () => {
  const shape = (v: TaskView) => v.feed.map((i) => i.kind);

  it("a step opening AFTER content starts a new phase tree - the transcript reads work, output, work", () => {
    const v = fold(
      initialView(detail()),
      stepRunning(1),
      ev({ type: "step.narration", seq: 2, markdown: "Here is the outline." }),
      stepRunning(3, { step_id: "s2", title: "Draft" }),
    );
    expect(shape(v)).toEqual(["execution", "segment", "execution"]);
    const [first, second] = v.feed.filter((i) => i.kind === "execution");
    expect(first.kind === "execution" && first.groups.some((g) => g.id === "s1")).toBe(true);
    expect(second.kind === "execution" && second.groups.some((g) => g.id === "s2")).toBe(true);
  });

  it("a parallel wave stays ONE tree: siblings opened before content share it, and ownership folds their late frames back", () => {
    const v = fold(
      initialView(detail()),
      stepRunning(1),
      stepRunning(2, { step_id: "s2", title: "Gather B" }),
      ev({ type: "step.narration", seq: 3, markdown: "Section one is in." }),
      // s1 finishes AFTER the narration - ownership routes it to the wave's tree, no mint.
      stepRunning(4, { state: "done" }),
      ev({
        type: "usage.recorded",
        seq: 5,
        step_id: "s2",
        model: "sonnet",
        cost_usd: "0.01",
        units: { input_token: 5, output_token: 5 },
      }),
    );
    expect(shape(v)).toEqual(["execution", "segment"]);
    const tree = exec(v);
    expect(tree?.groups.map((g) => g.id)).toEqual(["plan", "s1", "s2"]);
    expect(tree?.groups.find((g) => g.id === "s2")?.tokens).toBe(10);
  });

  it("a working-artifact chip row is content too - machinery after it opens a new phase", () => {
    const v = fold(
      initialView(detail()),
      stepRunning(1),
      ev({
        type: "artifact.produced",
        seq: 2,
        artifact_id: "w1",
        title: "01 Ports",
        artifact_type: "document",
        role: "working",
      }),
      stepRunning(3, { step_id: "s2", title: "Harmonize" }),
    );
    expect(shape(v)).toEqual(["execution", "working-artifacts", "execution"]);
  });

  it("settling settles EVERY phase tree, not only the frontier", () => {
    const v = fold(
      initialView(detail()),
      stepRunning(1),
      ev({ type: "step.narration", seq: 2, markdown: "First phase out." }),
      stepRunning(3, { step_id: "s2", title: "Draft" }),
      ev({ type: "task.delivered", seq: 4 }),
    );
    const trees = v.feed.filter((i) => i.kind === "execution");
    expect(trees).toHaveLength(2);
    expect(trees.every((i) => i.kind === "execution" && i.settled)).toBe(true);
  });

  it("both lanes split at the SAME boundary - the phase law keys on durable step frames only", () => {
    const durable: TaskEvent[] = [
      stepRunning(1),
      ev({ type: "step.narration", seq: 2, markdown: "The outline stands." }),
      stepRunning(3, { step_id: "s2", title: "Draft" }),
      ev({ type: "step.narration", seq: 4, markdown: "Drafted." }),
    ];
    const live: TaskEvent[] = [
      durable[0],
      ev({ type: "activity", kind: "model", phase: "start", label: "round 1" }),
      ev({ type: "message.start", agent: "writer" }),
      ev({ type: "message.delta", text: "The outline stands." }),
      ev({ type: "message.end" }),
      durable[1],
      durable[2],
      ev({
        type: "activity",
        kind: "tool",
        phase: "start",
        tool: "frame.write_file",
        tool_call_id: "tcX",
        label: "/work/d.md",
      }),
      ev({ type: "message.start", agent: "writer" }),
      ev({ type: "message.delta", text: "Drafted." }),
      ev({ type: "message.end" }),
      durable[3],
    ];
    const replayed = fold(initialView(detail()), ...durable);
    const streamed = fold(initialView(detail()), ...live);
    expect(shape(replayed)).toEqual(shape(streamed));
    expect(shape(replayed)).toEqual(["execution", "segment", "execution", "segment"]);
  });
});

describe("the turn receipt (round seven) - the epilogue tells the whole turn", () => {
  it("sums steps, agents, tokens, and wall-clock across ALL phase trees at delivery", () => {
    const v = fold(
      initialView(detail()),
      stepRunning(1, { at: "2026-08-30T10:00:00Z" }),
      ev({
        type: "usage.recorded",
        seq: 2,
        step_id: "s1",
        model: "m",
        cost_usd: "0.01",
        units: { input_token: 400, output_token: 100 },
      }),
      stepRunning(3, { state: "done", at: "2026-08-30T10:00:30Z" }),
      ev({ type: "step.narration", seq: 4, markdown: "Phase one out." }),
      stepRunning(5, {
        step_id: "s2",
        title: "Draft",
        agent: "typesetter",
        at: "2026-08-30T10:01:00Z",
      }),
      ev({
        type: "usage.recorded",
        seq: 6,
        step_id: "s2",
        model: "m",
        cost_usd: "0.01",
        units: { input_token: 1500, output_token: 500 },
      }),
      stepRunning(7, {
        step_id: "s2",
        title: "Draft",
        state: "done",
        agent: "typesetter",
        at: "2026-08-30T10:02:30Z",
      }),
      ev({ type: "task.delivered", seq: 8 }),
    );
    expect(v.feed.filter((i) => i.kind === "execution")).toHaveLength(2);
    const ep = v.feed.find((i) => i.kind === "epilogue");
    const receipt = ep?.kind === "epilogue" ? ep.receipt : undefined;
    expect(receipt).toMatchObject({
      steps: 2,
      agents: 2, // writer + typesetter: the plan group names no planner when no planner drafted the plan
      durationMs: 150_000, // 10:00:00 → 10:02:30, wall clock across both trees
      tokens: 2500,
    });
    // The x-ray rows (the hover ask): every group in feed order, plan phase included,
    // each carrying its own agent, span, and ledger.
    expect(receipt?.rows?.map((r) => [r.kind, r.title, r.agent, r.tokens ?? 0])).toEqual([
      ["plan", "Planning", undefined, 0],
      ["step", "Write", "writer", 500],
      ["step", "Draft", "typesetter", 2000],
    ]);
    expect(receipt?.rows?.[1].durationMs).toBe(30_000);
  });

  it("a replay with no anchors shows no timing and no tokens - never a guess", () => {
    const v = fold(initialView(detail()), ev({ type: "task.delivered", seq: 1 }));
    const ep = v.feed.find((i) => i.kind === "epilogue");
    expect(ep?.kind === "epilogue" && ep.receipt).toBeUndefined();
  });

  it("a partly done turn marks its epilogue partial; a done one does not", () => {
    const marker = (ending: "done" | "partial") =>
      fold(initialView(detail()), ev({ type: "task.delivered", seq: 1, ending })).feed.find(
        (i) => i.kind === "epilogue",
      );
    expect(marker("partial")).toMatchObject({ partial: true });
    expect(marker("done")).toMatchObject({ partial: false });
  });
});

describe("who drafted the plan is who the cast names", () => {
  const planned = (source?: string) =>
    ev({
      type: "task.planned",
      seq: 1,
      ...(source ? { route_source: source } : {}),
      route: "standard",
      plan: { steps: [{ id: "s1", title: "Answer", state: "pending", agent: "lead" }] },
    });
  const receiptAfter = (source?: string) => {
    const v = fold(
      initialView(detail()),
      planned(source),
      stepRunning(2, { step_id: "s1", title: "Answer", agent: "lead" }),
      stepRunning(3, { step_id: "s1", title: "Answer", agent: "lead", state: "done" }),
      ev({ type: "task.delivered", seq: 4 }),
    );
    const ep = v.feed.find((i) => i.kind === "epilogue");
    return {
      group: exec(v)?.groups.find((g) => g.kind === "plan"),
      receipt: ep?.kind === "epilogue" ? ep.receipt : undefined,
    };
  };

  it("a plan the Lead made counts no planner: one reply is one agent, and the Planned row wears no tag", () => {
    for (const source of ["lead", "dial"]) {
      const { group, receipt } = receiptAfter(source);
      expect(group?.agent).toBeUndefined();
      expect(receipt).toMatchObject({ steps: 1, agents: 1 });
    }
  });

  it("an older row whose planner drafted the plan keeps it in the cast", () => {
    for (const source of ["planner", "detector", "anchor", "hatch", undefined]) {
      const { group, receipt } = receiptAfter(source);
      expect(group?.agent).toBe("planner");
      expect(receipt).toMatchObject({ steps: 1, agents: 2 });
    }
  });
});

describe("a reloaded thread totals each turn exactly as the live one did", () => {
  // Three turns on one task. The reload seeds the view from the task read (turn 3), then replays
  // the whole record; the live tab folded the same frames as they came, from turn 1.
  const turnFrames = (n: number, seq: number): TaskEvent[] => [
    ...(n > 1
      ? [ev({ type: "task.continued", seq, message: `ask ${n}`, turn: n })]
      : [ev({ type: "task.created", seq, goal: "ask 1" })]),
    ev({
      type: "task.planned",
      seq: seq + 1,
      route_source: "lead",
      route: "express",
      plan: { steps: [] },
    }),
    stepRunning(seq + 2, {
      step_id: `s${n}`,
      title: `Reply ${n}`,
      agent: "lead",
      at: `2026-08-30T1${n}:00:00Z`,
    }),
    stepRunning(seq + 3, {
      step_id: `s${n}`,
      title: `Reply ${n}`,
      agent: "lead",
      state: "done",
      at: `2026-08-30T1${n}:00:03Z`,
    }),
    ev({ type: "task.delivered", seq: seq + 4 }),
  ];
  const record = [...turnFrames(1, 1), ...turnFrames(2, 10), ...turnFrames(3, 20)];
  const receipts = (v: TaskView) =>
    v.feed.flatMap((i) => (i.kind === "epilogue" ? [i.receipt] : []));

  it("live (from turn 1) and reloaded (seeded at turn 3) views fold to the same receipts", () => {
    const live = fold(initialView(detail({ turn: 1 })), ...record);
    const reloaded = fold(initialView(detail({ turn: 3, status: "delivered" })), ...record);
    expect(receipts(reloaded)).toEqual(receipts(live));
    expect(reloaded.feed).toEqual(live.feed);
    // Each turn's line is its own: one step, one agent, three seconds - never the thread's sum.
    for (const r of receipts(reloaded)) {
      expect(r).toMatchObject({ steps: 1, agents: 1, durationMs: 3000 });
    }
  });
});

describe("the rail's earlier plans are real re-plans only", () => {
  const leadStep = stepRunning(1, { step_id: "lead1", title: "Reply", agent: "lead" });
  const handOver = ev({
    type: "plan.edited",
    seq: 2,
    edit: { kind: "lead", reason: "the Lead ended with a plan" },
  });

  it("the Lead handing over to a plan supersedes nothing, however many steps joined", () => {
    for (const kind of ["lead", "expand", "clarify", "ask"]) {
      const v = fold(
        initialView(detail()),
        leadStep,
        ev({ type: "plan.edited", seq: 2, edit: { kind, reason: "" } }),
      );
      expect(v.planHistory).toEqual([]);
    }
    expect(fold(initialView(detail()), leadStep, handOver).steps).toHaveLength(1);
  });

  it("a real re-plan keeps the old plan, and a step it cut off never reads running", () => {
    const v = fold(
      initialView(detail()),
      leadStep,
      ev({ type: "plan.edited", seq: 2, edit: { kind: "replan", reason: "a step failed" } }),
    );
    expect(v.planHistory).toHaveLength(1);
    expect(v.planHistory[0].map((s) => s.state)).toEqual(["skipped"]);
    // The live plan keeps its own state: only the archived copy is frozen.
    expect(v.steps[0].state).toBe("running");
  });
});

describe("initialView", () => {
  it("seeds status, goal, and cost from the detail", () => {
    const v = initialView(detail());
    expect(v.status).toBe("created");
    expect(v.goal).toBe("write a memo");
    expect(v.totalUsd).toBe("0.0400");
    expect(v.delivered).toBe(false);
  });

  it("marks an already-delivered task delivered", () => {
    expect(initialView(detail({ status: "delivered" })).delivered).toBe(true);
  });
});

describe("seq dedup - what makes replay-from-zero safe", () => {
  it("returns the SAME reference for a replayed durable frame, so React skips the render", () => {
    const once = fold(
      initialView(detail()),
      ev({ type: "task.status", seq: 1, status: "planning" }),
    );
    const twice = reduce(once, ev({ type: "task.status", seq: 1, status: "planning" }));
    expect(twice).toBe(once);
  });

  it("ignores a frame at or below the applied seq", () => {
    const v = fold(initialView(detail()), ev({ type: "task.status", seq: 5, status: "running" }));
    expect(reduce(v, ev({ type: "task.status", seq: 3, status: "planning" }))).toBe(v);
    expect(v.status).toBe("running");
  });

  it("never double-counts usage across a reconnect replay", () => {
    const frame = ev({ type: "usage.recorded", seq: 2 });
    const v = fold(initialView(detail()), frame, frame);
    expect(v.usageCalls).toBe(1);
  });

  it("sums the live spend off usage frames - decimal strings and all", () => {
    // The header froze at the loader snapshot while calls counted up ("$0.0000 · 17 calls"):
    // cost_usd crosses the wire as a decimal STRING and was never folded.
    const v = fold(
      initialView(detail()),
      ev({ type: "usage.recorded", seq: 2, cost_usd: "0.0237" }),
      ev({ type: "usage.recorded", seq: 3, cost_usd: "0.0100" }),
    );
    expect(v.usageCalls).toBe(2);
    expect(v.usageUsd).toBeCloseTo(0.0337);
  });

  it("leaves usageUsd null until the first frame - the loader snapshot covers that paint", () => {
    expect(initialView(detail()).usageUsd).toBeNull();
  });
});

describe("the record's turning points", () => {
  it("task.delivered flips the view and appends the quiet EPILOGUE", () => {
    // One sentence-shaped block, never a green marker (the three stacked
    // colored markers read as a traffic light).
    const v = fold(
      initialView(detail()),
      ev({ type: "task.delivered", seq: 4, summary: "memo shipped" }),
    );
    expect(v.status).toBe("delivered");
    expect(v.delivered).toBe(true);
    expect(v.deliveredSummary).toBe("memo shipped");
    const last = v.feed.at(-1);
    expect(last).toMatchObject({ kind: "epilogue", delivered: true });
    expect(v.epilogueId).toBe(last?.kind === "epilogue" ? last.id : "");
  });

  it("the late receipt and the memories fold INTO the epilogue - one block, elaborated", () => {
    const v = fold(
      initialView(detail()),
      ev({ type: "task.delivered", seq: 4, summary: "done" }),
      ev({
        type: "verification.signed",
        seq: 5,
        verdict: "fail",
        suite: "document-suite",
        verifier_family: "google",
      }),
      ev({
        type: "memory.written",
        seq: 6,
        count: 2,
        items: [
          { id: "m1", kind: "preference", text: "Prefers tables over prose." },
          { id: "m2", kind: "fact", text: "Ships a phone hardware project." },
        ],
      }),
    );
    const eps = v.feed.filter((f) => f.kind === "epilogue");
    expect(eps).toHaveLength(1); // ONE block - no marker stack
    const ep = eps[0];
    if (ep?.kind !== "epilogue") throw new Error("no epilogue");
    expect(ep.verdict).toBe("fail");
    expect(ep.suite).toBe("document-suite");
    expect(ep.remembered.map((m) => m.text)).toEqual([
      "Prefers tables over prose.",
      "Ships a phone hardware project.",
    ]);
    expect(v.feed.some((f) => f.kind === "marker")).toBe(false);
  });

  it("gate.opened parks the task waiting; gate.resolved clears it", () => {
    const opened = fold(
      initialView(detail()),
      ev({
        type: "gate.opened",
        seq: 3,
        gate_id: "g1",
        title: "Two things before I start",
        questions: { title: "Two things before I start", questions: [] },
      }),
    );
    expect(opened.status).toBe("waiting");
    expect(opened.gates).toHaveLength(1);

    const resolved = reduce(opened, ev({ type: "gate.resolved", seq: 4, gate_id: "g1" }));
    expect(resolved.gates).toHaveLength(0);
  });

  it("an approve gate carries kind and reason into GateView, and its wait row reads approval (ADR-054)", () => {
    const opened = fold(
      initialView(detail()),
      ev({
        type: "gate.opened",
        seq: 3,
        gate_id: "g1",
        step_id: "s1",
        kind: "approve",
        title: "Allow Gmail: gmail_send_email?",
        reason: "Gmail: gmail_send_email\nto: a@b.c",
      }),
    );
    expect(opened.gates[0]).toMatchObject({
      kind: "approve",
      reason: "Gmail: gmail_send_email\nto: a@b.c",
    });
    const rows = opened.feed
      .flatMap((f) => (f.kind === "execution" ? f.groups : []))
      .flatMap((g) => g.rows);
    const wait = rows.find((r) => r.t === "tool" && r.id === "gate-g1");
    expect(wait && "summary" in wait ? wait.summary : "").toContain(
      "Waiting on your approval - Allow Gmail: gmail_send_email?",
    );
  });

  it("a connect gate carries its slug and why, waits as a connection, and resolves as one (ADR-054)", () => {
    const opened = fold(
      initialView(detail()),
      ev({
        type: "gate.opened",
        seq: 3,
        gate_id: "g1",
        step_id: "s1",
        kind: "connect",
        title: "Connect Notion to continue?",
        reason: "To create the page there.",
        connector_slug: "notion",
      }),
    );
    expect(opened.gates[0]).toMatchObject({
      kind: "connect",
      connector_slug: "notion",
    });
    const waitRow = opened.feed
      .flatMap((f) => (f.kind === "execution" ? f.groups : []))
      .flatMap((g) => g.rows)
      .find((r) => r.t === "tool" && r.id === "gate-g1");
    expect(waitRow && "summary" in waitRow ? waitRow.summary : "").toBe(
      "Waiting on you - Connect Notion to continue?",
    );
    const settled = reduce(
      opened,
      ev({ type: "gate.resolved", seq: 4, gate_id: "g1", action: "accept" }),
    );
    const done = settled.feed
      .flatMap((f) => (f.kind === "execution" ? f.groups : []))
      .flatMap((g) => g.rows)
      .find((r) => r.t === "tool" && r.id === "gate-g1");
    expect(done && "summary" in done ? done.summary : "").toBe("Connected - continuing");
  });

  it("resolve copy says allowed/denied for approve gates, answered/skipped for questions", () => {
    const base = fold(
      initialView(detail()),
      ev({
        type: "gate.opened",
        seq: 3,
        gate_id: "g1",
        step_id: "s1",
        kind: "approve",
        title: "Allow Gmail: gmail_send_email?",
        reason: "Gmail: gmail_send_email\nto: a@b.c",
      }),
    );
    const summaryOf = (view: ReturnType<typeof fold>) => {
      const rows = view.feed
        .flatMap((f) => (f.kind === "execution" ? f.groups : []))
        .flatMap((g) => g.rows);
      const wait = rows.find((r) => r.t === "tool" && r.id === "gate-g1");
      return wait && "summary" in wait ? (wait.summary ?? "") : "";
    };
    expect(
      summaryOf(
        reduce(
          base,
          ev({
            type: "gate.resolved",
            seq: 4,
            gate_id: "g1",
            action: "accept",
          }),
        ),
      ),
    ).toBe("You allowed it");
    expect(
      summaryOf(
        reduce(
          base,
          ev({
            type: "gate.resolved",
            seq: 4,
            gate_id: "g1",
            action: "decline",
          }),
        ),
      ),
    ).toBe("You denied it - continuing without");
  });

  it("on a Project task the wait row names whoever resolved the gate; a personal task says you", () => {
    // A teammate reads Ada's answer as Ada's, never as their own:
    // Bob's tree said "You answered" after Ada answered first.
    const opened = (d: Detail, kind: string) =>
      fold(
        initialView(d),
        ev({
          type: "gate.opened",
          seq: 3,
          gate_id: "g1",
          step_id: "s1",
          kind,
          title: "t",
          reason: "r",
        }),
      );
    const settled = (d: Detail, kind: string, action: string) => {
      const v = reduce(
        opened(d, kind),
        ev({
          type: "gate.resolved",
          seq: 4,
          gate_id: "g1",
          action,
          actor: { type: "user", id: "a-1", name: "Ada" },
        }),
      );
      const wait = v.feed
        .flatMap((f) => (f.kind === "execution" ? f.groups : []))
        .flatMap((g) => g.rows)
        .find((r) => r.t === "tool" && r.id === "gate-g1");
      return wait && "summary" in wait ? (wait.summary ?? "") : "";
    };
    const shared = detail({ project_id: "sp-1" });
    expect(settled(shared, "approve", "accept")).toBe("Ada allowed it");
    expect(settled(shared, "approve", "decline")).toBe("Ada denied it - continuing without");
    expect(settled(shared, "input", "accept")).toBe("Ada answered");
    expect(settled(detail(), "approve", "accept")).toBe("You allowed it");
    expect(settled(detail(), "input", "accept")).toBe("You answered");
    // A move in or out of a Project changes whose hand the tree names from then on.
    const moved = reduce(
      initialView(detail()),
      ev({ type: "task.moved", seq: 2, to_project_id: "sp-1" }),
    );
    expect(moved.shared).toBe(true);
    expect(reduce(moved, ev({ type: "task.moved", seq: 3, to_project_id: null })).shared).toBe(
      false,
    );
  });

  it("verification.signed stamps the artifact's verdict", () => {
    const v = fold(
      initialView(detail()),
      ev({
        type: "artifact.produced",
        seq: 6,
        artifact_id: "a1",
        title: "Memo",
        artifact_type: "document",
      }),
      ev({
        type: "verification.signed",
        seq: 7,
        verdict: "pass",
        artifact_id: "a1",
        suite: "document-suite",
        signature: "sig",
      }),
    );
    expect(v.artifacts).toHaveLength(1);
    expect(v.artifacts[0]).toMatchObject({ id: "a1", verdict: "pass" });
    expect(v.verification?.verdict).toBe("pass");
  });
});

describe("plan revisions", () => {
  it("plan.edited snapshots the superseded steps instead of erasing them", () => {
    const v = fold(
      initialView(detail()),
      ev({
        type: "step.state",
        seq: 2,
        step_id: "s1",
        title: "Draft",
        state: "running",
        attempt: 1,
      }),
      ev({ type: "plan.edited", seq: 3 }),
      ev({
        type: "step.state",
        seq: 4,
        step_id: "s2",
        title: "Rewrite",
        state: "pending",
        attempt: 1,
      }),
    );
    expect(v.planHistory).toHaveLength(1);
    expect(v.planHistory[0]).toHaveLength(1);
    expect(v.planHistory[0][0]).toMatchObject({ title: "Draft" });
    expect(v.steps.map((s) => s.title)).toContain("Rewrite");
  });

  it("a re-plan the system made for a steer says whose steer, in the plan group", () => {
    // The boundary re-plan (and fence 2's re-draft) record their reason; the
    // page must say it, not a bare "Plan revised".
    const notes = (v: TaskView) =>
      v.feed.flatMap((i) =>
        i.kind === "execution" ? i.groups.flatMap((g) => g.rows.filter((r) => r.t === "note")) : [],
      ) as Extract<ExecRow, { t: "note" }>[];
    const reason = "Re-planned for Bob's steer: 'Only cover German providers'";
    const steered = fold(
      initialView(detail()),
      ev({ type: "plan.edited", seq: 3, edit: { kind: "replan", reason } }),
    );
    expect(notes(steered).map((n) => n.text)).toEqual([reason]);
    const manual = fold(
      initialView(detail()),
      ev({
        type: "plan.edited",
        seq: 3,
        actor: { type: "user", id: "bob-id", name: "Bob" },
        edit: { kind: "replan", reason: "focus on pricing" },
      }),
    );
    expect(notes(manual).map((n) => n.text)).toEqual(["Plan revised"]);
  });
});

describe("escalations", () => {
  it("carries what the attempts reported, once, not once per retry", () => {
    const wall =
      "- **nothing was published.** no enabled model serves 'fast-executor'. Add one with `prismoid models add`.";
    const v = fold(
      initialView(detail()),
      ev({
        type: "escalation.raised",
        seq: 4,
        diagnosis: {
          what_failed: "Prismoid could not finish this part of the task",
          why: "Every attempt ended before the work was finished (3 tries).",
          attempts: 3,
          evidence: [wall, wall, wall, "- a second finding", "- a third", "- a fourth"],
          suggested_next: [],
        },
      }),
    );
    const last = v.feed.at(-1);
    if (last?.kind !== "escalation") throw new Error("expected the escalation card");
    expect(last.evidence).toEqual([
      "no enabled model serves 'fast-executor'. Add one with `prismoid models add`.",
      "a second finding",
      "a third",
    ]);
  });

  it("leaves out the agent's brief: the reason repeated, stop labels and steer re-run notes", () => {
    const v = fold(
      initialView(detail()),
      ev({
        type: "escalation.raised",
        seq: 4,
        diagnosis: {
          what_failed: "writer could not do this",
          why: "The source site is down.",
          attempts: 1,
          evidence: [
            "- **could not be done.** The source site is down.",
            "- **blocked (needs a login).** The archive asks for an account.",
            "**Ada steered** after your last attempt finished: shorter\n\nYour earlier output is superseded: redo it in /work and publish again.",
          ],
          suggested_next: [],
        },
      }),
    );
    const last = v.feed.at(-1);
    if (last?.kind !== "escalation") throw new Error("expected the escalation card");
    expect(last.reason).toBe("The source site is down.");
    expect(last.evidence).toEqual(["The archive asks for an account."]);
  });

  it("folds the diagnosis into a card carrying the REAL reason, never the preamble", () => {
    // `why` leads with "…The last, because:\n<finding>". The old one-line marker took line
    // one, so the page showed a red colon and no reason at all (the
    // consciousness-theory task). The card takes what follows the colon, made legible.
    const v = fold(
      initialView(detail()),
      ev({
        type: "escalation.raised",
        seq: 4,
        diagnosis: {
          what_failed: "writer could not produce a deliverable",
          why: "3 attempts, each one failed. The last, because:\n- **nothing was published.** Model token limit (provider default) exceeded while generating a tool call.",
          attempts: 3,
          evidence: [],
          suggested_next: ["Re-run with a narrower goal."],
        },
      }),
    );
    const last = v.feed.at(-1);
    expect(last?.kind).toBe("escalation");
    if (last?.kind === "escalation") {
      expect(last.headline).toBe("writer could not produce a deliverable");
      expect(last.reason).toBe(
        "Model token limit (provider default) exceeded while generating a tool call.",
      );
      expect(last.tries).toEqual(["Re-run with a narrower goal."]);
    }
  });

  it("step.state escalated adds NO marker - the tree group and the card carry it", () => {
    // Two red lines for one event is how the transcript stopped reading as English. The
    // step's story lives in its execution group now; the escalation card stays top-level.
    const v = fold(
      initialView(detail()),
      ev({
        type: "step.state",
        seq: 3,
        step_id: "s1",
        title: "draft",
        state: "escalated",
        attempt: 3,
        agent: "writer",
        detail: "",
      }),
    );
    expect(v.feed).toHaveLength(1);
    const item = v.feed[0];
    expect(item.kind).toBe("execution");
    if (item.kind === "execution") {
      expect(item.groups.find((g) => g.id === "s1")?.state).toBe("escalated");
    }
    expect(v.steps[0]?.state).toBe("escalated");
  });

  it("a retrying step becomes a warning NOTE inside its group, in English", () => {
    const v = fold(
      initialView(detail()),
      ev({
        type: "step.state",
        seq: 3,
        step_id: "s1",
        title: "draft",
        state: "retrying",
        attempt: 1,
        agent: "writer",
        detail:
          "- **nothing was published.** The manifest is unreadable - write the file and publish again. Second sentence.",
      }),
    );
    expect(v.feed).toHaveLength(1); // the execution item - never a top-level marker
    const item = v.feed[0];
    expect(item.kind).toBe("execution");
    if (item.kind !== "execution") return;
    const group = item.groups.find((g) => g.id === "s1");
    const note = group?.rows.find((r) => r.t === "note");
    expect(note && note.t === "note" && note.tone).toBe("warning");
    expect(note && note.t === "note" && note.text).toBe(
      "Attempt 1 didn't produce the deliverable - taking another pass: The manifest is unreadable - write the file and publish again.",
    );
  });
});

describe("a retrying step's note", () => {
  it("counts attempts without a hard-coded ceiling: infra retries run past the model's three", () => {
    const v = fold(
      initialView(detail()),
      ev({
        type: "step.state",
        seq: 3,
        step_id: "s1",
        title: "draft",
        state: "retrying",
        attempt: 4,
        agent: "writer",
        detail: "",
      }),
    );
    const item = v.feed[0];
    if (item.kind !== "execution") throw new Error("expected the execution item");
    const note = item.groups.find((g) => g.id === "s1")?.rows.find((r) => r.t === "note");
    expect(note && note.t === "note" && note.text).toBe(
      "Attempt 4 didn't produce the deliverable - taking another pass",
    );
  });
});

describe("the context ledger (context.assembled / memory.written)", () => {
  it("context.assembled feeds the rail AND the tree - never a top-level feed item", () => {
    // The rail keeps the full lane detail; the tree's plan group gets the one-line fact
    // ("what does it already know" is part of the execution story). No
    // top-level marker - the machinery stays inside the execution item.
    const v = fold(
      initialView(detail()),
      ev({
        type: "context.assembled",
        seq: 2,
        lanes: [
          {
            lane: "user",
            tokens: 17,
            truncated: true,
            items: [{ preview: "Prefers numbers tables.", memory_id: "m1" }],
          },
        ],
        total_tokens: 17,
        memory_ids: ["m1"],
      }),
    );
    expect(v.context?.totalTokens).toBe(17);
    expect(v.context?.lanes[0]?.items[0]?.preview).toBe("Prefers numbers tables.");
    expect(v.feed).toHaveLength(1);
    const item = v.feed[0];
    expect(item.kind).toBe("execution");
    if (item.kind === "execution") {
      const plan = item.groups.find((g) => g.id === "plan");
      const row = plan?.rows.find((r) => r.t === "tool");
      expect(row?.t === "tool" && row.summary).toBe(
        "Gathered context - 17 tok · 1 lane, 1 trimmed",
      );
    }
    // Replay dedup: the same durable frame folds to the identical view.
    expect(reduce(v, ev({ type: "context.assembled", seq: 2, lanes: [], total_tokens: 0 }))).toBe(
      v,
    );
  });

  it("memory.written marks the transcript only when something was kept", () => {
    const silent = fold(
      initialView(detail()),
      ev({ type: "memory.written", seq: 3, count: 0, items: [] }),
    );
    expect(silent.feed).toHaveLength(0);

    const kept = fold(
      initialView(detail()),
      ev({
        type: "memory.written",
        seq: 3,
        count: 1,
        items: [{ id: "m1", kind: "preference", text: "Prefers concise memos." }],
      }),
    );
    const last = kept.feed.at(-1);
    expect(last?.kind).toBe("marker");
    if (last?.kind === "marker") {
      expect(last.glyph).toBe("memory");
      expect(last.text).toBe("Remembered: Prefers concise memos.");
    }
  });
});

describe("the message lane (§8.6)", () => {
  it("start → deltas → end folds one segment, appended in order, then closed", () => {
    const v = fold(
      initialView(detail()),
      ev({ type: "message.start", agent: "writer" }),
      ev({ type: "message.delta", text: "Hello " }),
      ev({ type: "message.delta", text: "world" }),
      ev({ type: "message.end" }),
    );
    const segments = v.feed.filter((f) => f.kind === "segment");
    expect(segments).toHaveLength(1);
    expect(segments[0]).toMatchObject({ markdown: "Hello world", live: false });
  });

  it("streaming clears progress stretch so the ticker does not keep a dead-air clock", () => {
    const withProgress = fold(
      initialView(detail()),
      ev({ type: "progress", phase: "thinking", elapsed_s: 12 }),
    );
    expect(withProgress.live).toEqual({ phase: "thinking", elapsed_s: 12 });

    const streaming = fold(
      withProgress,
      ev({ type: "message.start", agent: "writer" }),
      ev({ type: "message.delta", text: "Hi" }),
    );
    expect(streaming.live).toBeNull();
  });

  it("a narration matching the live segment closes it without a second bubble (m10.2)", () => {
    const v = fold(
      initialView(detail()),
      ev({ type: "message.start", agent: "writer" }),
      ev({ type: "message.delta", text: "The answer" }),
      ev({ type: "step.narration", seq: 5, markdown: "The answer" }),
    );
    const segments = v.feed.filter((f) => f.kind === "segment");
    expect(segments).toHaveLength(1);
    expect(segments[0]).toMatchObject({ markdown: "The answer", live: false });
  });

  it("a narration with different text still closes the live segment and lands the answer", () => {
    const v = fold(
      initialView(detail()),
      ev({ type: "message.start", agent: "writer" }),
      ev({ type: "message.delta", text: "draft…" }),
      ev({ type: "step.narration", seq: 5, markdown: "The final answer." }),
    );
    const segments = v.feed.filter((f) => f.kind === "segment");
    expect(segments).toHaveLength(2);
    expect(segments.every((s) => !s.live)).toBe(true);
    expect(segments.at(-1)).toMatchObject({ markdown: "The final answer." });
  });

  it("the two-lanes echo folds at message.end - found live 2026-08-29, by the email cards' height", () => {
    // Lane-1 flushes immediately; the Lane-2 batch is coalesced (≤10 fps). The narration can
    // therefore land FIRST, fold into the barely-started live segment, and settle it - then
    // the echo batch re-accumulates the identical text in a fresh bubble. Before the fix the
    // thread showed the answer twice (4 email cards for 2 drafts, live on the dev box).
    const answer = "```email\nSubject: Hi\n\nBody.\n```";
    const v = fold(
      initialView(detail()),
      ev({ type: "message.start", agent: "answerer" }),
      ev({ type: "step.narration", seq: 5, markdown: answer }),
      ev({ type: "message.delta", text: answer.slice(0, 12) }),
      ev({ type: "message.delta", text: answer.slice(12) }),
      ev({ type: "message.end" }),
    );
    const segments = v.feed.filter((f) => f.kind === "segment");
    expect(segments).toHaveLength(1);
    expect(segments[0]).toMatchObject({ markdown: answer, live: false });
  });

  it("a whole late batch (start included) after the narration folds the same way", () => {
    const answer = "The final answer.";
    const v = fold(
      initialView(detail()),
      ev({ type: "step.narration", seq: 5, markdown: answer }),
      ev({ type: "message.start", agent: "answerer" }),
      ev({ type: "message.delta", text: answer }),
      ev({ type: "message.end" }),
    );
    const segments = v.feed.filter((f) => f.kind === "segment");
    expect(segments).toHaveLength(1);
    expect(segments[0]).toMatchObject({ markdown: answer, live: false });
  });

  it("an empty ghost bubble (start+end, deltas lost) vanishes at end", () => {
    const v = fold(
      initialView(detail()),
      ev({ type: "step.narration", seq: 5, markdown: "The answer." }),
      ev({ type: "message.start", agent: "answerer" }),
      ev({ type: "message.end" }),
    );
    const segments = v.feed.filter((f) => f.kind === "segment");
    expect(segments).toHaveLength(1);
    expect(segments[0]).toMatchObject({ markdown: "The answer." });
  });

  it("a genuinely NEW second message keeps its own bubble - dedup is containment, not adjacency", () => {
    const v = fold(
      initialView(detail()),
      ev({ type: "message.start", agent: "writer" }),
      ev({ type: "message.delta", text: "First thought." }),
      ev({ type: "message.end" }),
      ev({ type: "message.start", agent: "writer" }),
      ev({ type: "message.delta", text: "A different second thought." }),
      ev({ type: "message.end" }),
    );
    const segments = v.feed.filter((f) => f.kind === "segment");
    expect(segments).toHaveLength(2);
  });

  it("the dedup never crosses a turn - a follow-up's short answer survives (2026-08-29)", () => {
    // Turn 2's whole reply ("Done.") is contained in turn 1's long answer; the backward
    // scan used to walk over the user's bubble, call the two one answer, and splice the
    // follow-up's only segment away - the new turn showed no reply at all.
    const v = fold(
      initialView(detail()),
      ev({
        type: "step.narration",
        seq: 2,
        markdown: "All wrapped up. Done. The memo is filed where you asked.",
      }),
      ev({
        type: "task.continued",
        seq: 3,
        message: "and the appendix?",
        turn: 2,
      }),
      ev({ type: "message.start", agent: "answerer" }),
      ev({ type: "message.delta", text: "Done." }),
      ev({ type: "message.end" }),
    );
    const segments = v.feed.filter((f) => f.kind === "segment");
    expect(segments).toHaveLength(2);
    expect(segments.at(-1)).toMatchObject({ markdown: "Done.", live: false });
  });

  it("a multi-step summary the feed already told piecewise never re-appends (scatter, 2026-08-29)", () => {
    const v = fold(
      initialView(detail()),
      ev({
        type: "step.narration",
        seq: 4,
        markdown: "Territory mapped - 3 seats.",
      }),
      ev({ type: "step.narration", seq: 5, markdown: "Gathered 7 record(s)." }),
      ev({
        type: "step.narration",
        seq: 6,
        markdown: "The findings:\n\n| a | b |",
      }),
      ev({
        type: "task.delivered",
        seq: 7,
        summary:
          "Territory mapped - 3 seats.\n\nGathered 7 record(s).\n\nThe findings:\n\n| a | b |",
        artifact_ids: [],
      }),
    );
    const segments = v.feed.filter((f) => f.kind === "segment");
    expect(segments).toHaveLength(3);
    // ...and none of them SWELLED into the join (fold-first once did exactly that: the
    // last segment became receipts+digest and the thread told the story twice).
    expect(segments.map((s) => (s as { markdown: string }).markdown)).toEqual([
      "Territory mapped - 3 seats.",
      "Gathered 7 record(s).",
      "The findings:\n\n| a | b |",
    ]);
    expect(v.status).toBe("delivered");
  });

  it("a summary carrying genuinely NEW text still lands", () => {
    const v = fold(
      initialView(detail()),
      ev({ type: "step.narration", seq: 4, markdown: "Step one done." }),
      ev({
        type: "task.delivered",
        seq: 5,
        summary: "Step one done.\n\nAnd the part only the summary says.",
        artifact_ids: [],
      }),
    );
    const segments = v.feed.filter((f) => f.kind === "segment");
    expect(segments.map((s) => s.markdown).join("|")).toContain("only the summary says");
  });

  it("a narration after message.end (closed segment) still folds - no second bubble", () => {
    // The screenshot bug: end closes the segment before the durable narration lands; comparing
    // only against a still-live segment re-appended the whole answer.
    const v = fold(
      initialView(detail()),
      ev({ type: "message.start", agent: "writer" }),
      ev({ type: "message.delta", text: "The answer" }),
      ev({ type: "message.end" }),
      ev({ type: "step.narration", seq: 5, markdown: "The answer" }),
    );
    const segments = v.feed.filter((f) => f.kind === "segment");
    expect(segments).toHaveLength(1);
  });

  it("a narration that contains the streamed text upgrades the bubble in place", () => {
    const v = fold(
      initialView(detail()),
      ev({ type: "message.start", agent: "writer" }),
      ev({ type: "message.delta", text: "> quote\n\nThe math holds." }),
      ev({ type: "message.end" }),
      ev({
        type: "step.narration",
        seq: 5,
        markdown: "Done. > quote\n\nThe math holds.",
      }),
    );
    const segments = v.feed.filter((f) => f.kind === "segment");
    expect(segments).toHaveLength(1);
    expect(segments[0]).toMatchObject({
      markdown: "Done. > quote\n\nThe math holds.",
      live: false,
    });
  });

  it("markers between the stream and its narration don't break the fold", () => {
    const v = fold(
      initialView(detail()),
      ev({ type: "message.start", agent: "writer" }),
      ev({ type: "message.delta", text: "The answer" }),
      ev({ type: "message.end" }),
      ev({
        type: "artifact.produced",
        seq: 5,
        artifact_id: "a1",
        title: "Memo",
        artifact_type: "document",
      }),
      ev({ type: "step.narration", seq: 6, markdown: "The answer" }),
    );
    expect(v.feed.filter((f) => f.kind === "segment")).toHaveLength(1);
  });
});

describe("the delivered stamp", () => {
  it("a summary the feed already shows folds away - marker stays a one-word stamp", () => {
    const v = fold(
      initialView(detail()),
      ev({ type: "message.start", agent: "writer" }),
      ev({ type: "message.delta", text: "**Done.** The memo is published." }),
      ev({ type: "message.end" }),
      ev({
        type: "step.narration",
        seq: 5,
        markdown: "**Done.** The memo is published.",
      }),
      ev({
        type: "task.delivered",
        seq: 6,
        summary: "**Done.** The memo is published.",
      }),
    );
    expect(v.feed.filter((f) => f.kind === "segment")).toHaveLength(1);
    const last = v.feed.at(-1);
    expect(last).toMatchObject({ kind: "epilogue", delivered: true });
  });

  it("a summary the feed does NOT show lands as rendered markdown, then the stamp", () => {
    const v = fold(
      initialView(detail()),
      ev({ type: "task.delivered", seq: 4, summary: "memo shipped" }),
    );
    const segments = v.feed.filter((f) => f.kind === "segment");
    expect(segments).toHaveLength(1);
    expect(segments[0]).toMatchObject({ markdown: "memo shipped" });
    expect(v.feed.at(-1)).toMatchObject({ kind: "epilogue", delivered: true });
  });

  it("turn 2's summary repeating turn 1's text still lands - vouching is per turn (2026-08-29)", () => {
    // The cross-turn sibling of the message.end splice: with no segment of its own,
    // turn 2's delivery used to match its summary against turn 1's prose and show
    // nothing at all for the turn that repeated an answer.
    const v = fold(
      initialView(detail()),
      ev({
        type: "step.narration",
        seq: 2,
        markdown: "The memo is filed. Done.",
      }),
      ev({
        type: "task.delivered",
        seq: 3,
        summary: "The memo is filed. Done.",
      }),
      ev({ type: "task.continued", seq: 4, message: "file it again", turn: 2 }),
      ev({ type: "task.delivered", seq: 5, summary: "Done." }),
    );
    const segments = v.feed.filter((f) => f.kind === "segment");
    expect(segments).toHaveLength(2);
    expect(segments.at(-1)).toMatchObject({ markdown: "Done." });
    // …and turn 1's bubble kept its own text - nothing folded across the boundary.
    expect(segments[0]).toMatchObject({ markdown: "The memo is filed. Done." });
  });
});

describe("the draft - the streaming answer assembled from artifact.delta", () => {
  const base = () => initialView(detail({ status: "running" }));

  it("appends contiguous offsets and overlays idempotent resends", () => {
    let v = fold(
      base(),
      ev({
        type: "artifact.delta",
        path: "/work/d.md",
        offset: 0,
        text: "# Title\n\nAlpha ",
      }),
      ev({
        type: "artifact.delta",
        path: "/work/d.md",
        offset: 15,
        text: "beta",
      }),
    );
    expect(v.draft).toEqual({
      path: "/work/d.md",
      text: "# Title\n\nAlpha beta",
    });
    // A resent frame overlays instead of duplicating.
    v = fold(
      v,
      ev({
        type: "artifact.delta",
        path: "/work/d.md",
        offset: 15,
        text: "beta",
      }),
    );
    expect(v.draft?.text).toBe("# Title\n\nAlpha beta");
  });

  it("a dropped frame leaves a visible seam, never silent corruption", () => {
    const v = fold(
      base(),
      ev({
        type: "artifact.delta",
        path: "/work/d.md",
        offset: 0,
        text: "start",
      }),
      ev({
        type: "artifact.delta",
        path: "/work/d.md",
        offset: 99,
        text: "tail",
      }),
    );
    expect(v.draft?.text).toBe("start … tail");
  });

  it("legacy whole-current previews (offset 0) keep the longer text", () => {
    const v = fold(
      base(),
      ev({
        type: "artifact.delta",
        path: "/work/d.md",
        offset: 0,
        text: "one two three",
      }),
      ev({
        type: "artifact.delta",
        path: "/work/d.md",
        offset: 0,
        text: "one",
      }),
    );
    expect(v.draft?.text).toBe("one two three");
  });

  it("a durable snapshot heals the seam when it is longer", () => {
    let v = fold(
      base(),
      ev({
        type: "artifact.delta",
        path: "/work/d.md",
        offset: 0,
        text: "start",
      }),
      ev({
        type: "artifact.delta",
        path: "/work/d.md",
        offset: 99,
        text: "tail",
      }),
    );
    v = fold(
      v,
      ev({
        type: "artifact.snapshot",
        seq: 7,
        path: "/work/d.md",
        offset: 0,
        content: "start middle tail and much more of the document",
      }),
    );
    expect(v.draft?.text).toBe("start middle tail and much more of the document");
    // …but a shorter snapshot never eats streamed text.
    v = fold(
      v,
      ev({
        type: "artifact.snapshot",
        seq: 8,
        path: "/work/d.md",
        offset: 0,
        content: "start",
      }),
    );
    expect(v.draft?.text).toBe("start middle tail and much more of the document");
  });

  it("artifact.produced retires the draft - the real card takes over", () => {
    let v = fold(
      base(),
      ev({
        type: "artifact.delta",
        path: "/work/d.md",
        offset: 0,
        text: "the doc",
      }),
    );
    expect(v.draft).not.toBeNull();
    v = fold(
      v,
      ev({
        type: "artifact.produced",
        seq: 9,
        artifact_id: "a-1",
        title: "The Doc",
        artifact_type: "document",
      }),
    );
    expect(v.draft).toBeNull();
  });

  it("terminal statuses clear a stranded draft", () => {
    const v = fold(
      base(),
      ev({
        type: "artifact.delta",
        path: "/work/d.md",
        offset: 0,
        text: "half a doc",
      }),
      ev({ type: "task.status", seq: 10, status: "failed" }),
    );
    expect(v.draft).toBeNull();
  });
});

describe("draft on durable replay (a delivered task revisited)", () => {
  it("a full replay that carries snapshots still ends with no draft", () => {
    // The inbox preview and a page refresh replay ONLY durable frames, in record order:
    // snapshot(s) arrive before artifact.produced and task.delivered. Whatever the order
    // builds up, the end state must never show a draft card for finished work.
    const v = fold(
      initialView(detail({ status: "delivered" })),
      ev({
        type: "artifact.snapshot",
        seq: 5,
        path: "/work/d.md",
        offset: 0,
        content: "# The doc…",
      }),
      ev({
        type: "artifact.produced",
        seq: 6,
        artifact_id: "a1",
        title: "The Doc",
        artifact_type: "document",
      }),
      ev({ type: "task.delivered", seq: 7, summary: "done" }),
    );
    expect(v.draft).toBeNull();
    expect(v.delivered).toBe(true);
  });
});

describe("stragglers after settlement are dropped (2026-08-09)", () => {
  // A settled turn (execId null + terminal) must ignore a late ephemeral frame - the same
  // drop the `activity` case always made, now for message.*/progress/artifact.delta too.
  const settled = () =>
    fold(
      initialView(detail({ status: "running" })),
      ev({ type: "message.start", seq: 1, agent: "writer" }),
      ev({ type: "message.delta", text: "The answer." }),
      ev({ type: "message.end" }),
      ev({ type: "task.delivered", seq: 2, summary: "The answer." }),
    );

  it("a late message.delta mints no ghost typing bubble", () => {
    const before = settled();
    const segs = before.feed.filter((f) => f.kind === "segment").length;
    const v = fold(before, ev({ type: "message.delta", text: " …and more" }));
    expect(v.feed.filter((f) => f.kind === "segment").length).toBe(segs);
  });

  it("a late progress does not resurrect the live ticker", () => {
    const v = fold(
      settled(),
      ev({
        type: "progress",
        phase: "thinking",
        elapsed_s: 5,
        tokens_so_far: 0,
        detail: "",
      }),
    );
    expect(v.live).toBeNull();
  });

  it("a late artifact.delta does not resurrect the retired draft", () => {
    const v = fold(
      settled(),
      ev({ type: "artifact.delta", path: "/work/d.md", offset: 0, text: "x" }),
    );
    expect(v.draft).toBeNull();
  });

  it("but message.end still closes a segment a no-summary delivery left open", () => {
    // Exempt from the drop on purpose: end CLOSES liveness, it never resurrects it.
    const v = fold(
      initialView(detail({ status: "running" })),
      ev({ type: "message.start", seq: 1, agent: "writer" }),
      ev({ type: "message.delta", text: "streaming…" }),
      ev({ type: "task.delivered", seq: 2 }), // no summary → the live segment stays open
      ev({ type: "message.end" }),
    );
    expect(v.feed.filter((f) => f.kind === "segment" && f.live)).toHaveLength(0);
  });
});

describe("the execution tree (the progress tree's fold, 2026-08-07)", () => {
  it("task.planned mints the tree with the plan's own steps as the checklist", () => {
    const v = fold(
      initialView(detail()),
      ev({
        type: "task.planned",
        seq: 2,
        plan: {
          steps: [
            { id: "s1", title: "Research the brief", state: "pending" },
            { id: "s2", title: "Write the memo", state: "pending" },
          ],
        },
      }),
    );
    const item = exec(v);
    expect(item).toBeDefined();
    expect(v.execId).toBe(item?.id);
    const plan = item?.groups.find((g) => g.id === "plan");
    expect(plan?.kind).toBe("plan");
    expect(plan?.checklist?.map((c) => c.title)).toEqual(["Research the brief", "Write the memo"]);
    // No "Plan ready" marker anywhere - the tree replaced it.
    expect(v.feed.some((i) => i.kind === "marker")).toBe(false);
  });

  it("a Machine phase's start carries the server's wait bound onto its pending row", () => {
    const row = (wait_s?: number) =>
      exec(
        fold(
          initialView(detail()),
          ev({
            type: "activity",
            kind: "engine",
            phase: "start",
            tool: "machine",
            label: "Starting the Machine",
            step_id: "s1",
            ...(wait_s ? { wait_s } : {}),
          }),
        ),
      )?.groups[0].rows[0];
    expect(row(600)).toMatchObject({ tool: "machine", pending: true, waitS: 600 });
    expect(row()).not.toHaveProperty("waitS"); // an older event states none
  });

  it("the minting frame anchors the plan group's clock (the pre-step tick, 2026-08-29)", () => {
    // Before any step runs the tail clock reads the first group with a startedAt; the
    // plan group seeded none, so the whole clarifying/planning stretch fell to the
    // heartbeat's once-per-frame jumps. The minting frame's own `at` is the anchor -
    // and the FIRST one wins: a later fact never re-anchors a running clock.
    const v = fold(
      initialView(detail()),
      ev({
        type: "activity",
        kind: "phase",
        phase: "start",
        tool: "clarifier",
        label: "Reviewing your request",
        at: "2026-08-29T10:00:00Z",
      }),
      ev({
        type: "activity",
        kind: "phase",
        phase: "start",
        tool: "namer",
        label: "Naming the task",
        at: "2026-08-29T10:00:02Z",
      }),
    );
    const plan = exec(v)?.groups.find((g) => g.id === "plan");
    expect(plan?.startedAt).toBe("2026-08-29T10:00:00Z");
    // The durable lane anchors too - a replay's first plan-group frame stamps its own at.
    const planned = fold(
      initialView(detail()),
      ev({
        type: "task.planned",
        seq: 2,
        at: "2026-08-29T10:00:05Z",
        plan: { steps: [] },
      }),
    );
    expect(exec(planned)?.groups.find((g) => g.id === "plan")?.startedAt).toBe(
      "2026-08-29T10:00:05Z",
    );
  });

  it("task.planned seeds the rail's steps too - why survives a from-birth watch (ADR-045)", () => {
    // A task watched from its creation builds v.steps purely from frames. step.state carries
    // agent and state but not `why`, so the planned frame must merge the plan into v.steps -
    // otherwise the routing reason would render only after a reload, never on the first watch.
    const v = fold(
      initialView(detail()),
      ev({
        type: "task.planned",
        seq: 2,
        plan: {
          steps: [
            {
              id: "s1",
              title: "Review the MSA",
              state: "pending",
              agent: "contract-reviewer",
              why: "built to read an MSA clause by clause",
            },
          ],
        },
      }),
      ev({
        type: "step.state",
        seq: 3,
        step_id: "s1",
        title: "Review the MSA",
        state: "running",
        agent: "contract-reviewer",
        attempt: 1,
      }),
    );
    const step = v.steps.find((s) => s.id === "s1");
    expect(step?.why).toBe("built to read an MSA clause by clause");
    expect(step?.state).toBe("running"); // the later frame still advances state
  });

  it("step.state ticks the checklist, anchors timing, and snapshots the group", () => {
    const v = fold(
      initialView(detail()),
      ev({
        type: "task.planned",
        seq: 2,
        plan: { steps: [{ id: "s1", title: "Write", state: "pending" }] },
      }),
      stepRunning(3, { at: "2026-08-07T10:00:00Z" }),
      ev({
        type: "step.state",
        seq: 4,
        step_id: "s1",
        title: "Write",
        state: "done",
        attempt: 1,
        agent: "writer",
        detail: "",
        at: "2026-08-07T10:00:41Z",
      }),
    );
    const item = exec(v);
    const plan = item?.groups.find((g) => g.id === "plan");
    expect(plan?.checklist?.[0]?.state).toBe("done");
    const step = item?.groups.find((g) => g.id === "s1");
    expect(step?.state).toBe("done");
    expect(step?.startedAt).toBe("2026-08-07T10:00:00Z");
    expect(step?.endedAt).toBe("2026-08-07T10:00:41Z");
  });

  it("tool.called lands in ITS step's group; a step-less frame falls to the tail group", () => {
    const v = fold(
      initialView(detail()),
      stepRunning(2),
      ev({
        type: "tool.called",
        seq: 3,
        step_id: "s1",
        tool: "frame.read_file",
        summary: "/task/inputs/brief.md",
        ok: true,
        duration_ms: 220,
      }),
      ev({
        type: "tool.called",
        seq: 4,
        tool: "frame.shell",
        summary: "python3 build.py",
        ok: true,
      }),
    );
    const step = exec(v)?.groups.find((g) => g.id === "s1");
    expect(step?.rows.map((r) => (r.t === "tool" ? r.tool : r.t))).toEqual([
      "frame.read_file",
      "frame.shell", // step_id-less → the tail step group
    ]);
    const read = step?.rows[0];
    expect(read?.t === "tool" && read.durationMs).toBe(220);
  });

  it("delivery settles the tree; the next turn minted by task.continued grows a SECOND one", () => {
    const v = fold(
      initialView(detail()),
      ev({
        type: "step.state",
        seq: 2,
        step_id: "s1",
        title: "Write",
        state: "done",
        attempt: 1,
        agent: "writer",
        detail: "",
      }),
      ev({ type: "task.delivered", seq: 3, summary: "done" }),
      ev({ type: "task.continued", seq: 4, message: "again", turn: 2 }),
      stepRunning(5, { step_id: "s2", title: "Revise" }),
    );
    const items = v.feed.filter((i) => i.kind === "execution");
    expect(items).toHaveLength(2);
    expect(items[0]?.kind === "execution" && items[0].settled).toBe(true);
    expect(items[1]?.kind === "execution" && items[1].settled).toBe(false);
    // The first tree's groups are frozen snapshots - the turn reset didn't blank them.
    expect(
      items[0]?.kind === "execution" && items[0].groups.find((g) => g.id === "s1")?.state,
    ).toBe("done");
  });

  it("live activity start→end and the durable tool.called land on ONE row (the reconcile)", () => {
    const stepFrame = stepRunning(2);
    const durableTool = ev({
      type: "tool.called",
      seq: 3,
      step_id: "s1",
      tool: "frame.read_file",
      summary: "/task/inputs/brief.md",
      ok: true,
      duration_ms: 220,
      tool_call_id: "tc9",
    });
    // Live lane: the ephemeral start (pending spinner), the ephemeral end, then the record.
    const streamed = fold(
      initialView(detail()),
      stepFrame,
      ev({
        type: "activity",
        kind: "tool",
        phase: "start",
        step_id: "s1",
        tool: "frame.read_file",
        label: "/task/inputs/brief.md",
        tool_call_id: "tc9",
      }),
      ev({
        type: "activity",
        kind: "tool",
        phase: "end",
        step_id: "s1",
        tool: "frame.read_file",
        tool_call_id: "tc9",
        ok: true,
        duration_ms: 220,
      }),
      durableTool,
    );
    // Replay lane: durable frames only.
    const replayed = fold(initialView(detail()), stepFrame, durableTool);

    const rows = (v: TaskView) => {
      const item = v.feed.find((i) => i.kind === "execution");
      const g = item?.kind === "execution" ? item.groups.find((x) => x.id === "s1") : undefined;
      return (g?.rows ?? []).map((r) =>
        r.t === "tool"
          ? {
              id: r.id,
              tool: r.tool,
              summary: r.summary,
              ok: r.ok,
              durationMs: r.durationMs,
              pending: r.pending,
            }
          : r,
      );
    };
    expect(rows(streamed)).toHaveLength(1);
    expect(rows(streamed)).toEqual(rows(replayed));
    const only = rows(streamed)[0];
    expect(only && "pending" in only && only.pending).toBe(false);
    expect(only && "summary" in only && only.summary).toBe("/task/inputs/brief.md");
  });

  it("a model round rows up as Thinking… and settles with its duration", () => {
    const v = fold(
      initialView(detail()),
      stepRunning(2),
      ev({
        type: "activity",
        kind: "model",
        phase: "start",
        step_id: "s1",
        label: "round 1",
      }),
      ev({
        type: "activity",
        kind: "thinking",
        phase: "delta",
        step_id: "s1",
        text: "Outline first…",
      }),
      ev({
        type: "activity",
        kind: "model",
        phase: "end",
        step_id: "s1",
        duration_ms: 3400,
      }),
    );
    const item = v.feed.find((i) => i.kind === "execution");
    const g = item?.kind === "execution" ? item.groups.find((x) => x.id === "s1") : undefined;
    const round = g?.rows.find((r) => r.t === "round");
    expect(round?.t === "round" && round.pending).toBe(false);
    expect(round?.t === "round" && round.durationMs).toBe(3400);
    expect(round?.t === "round" && round.thinkingText).toBe("Outline first…");
  });

  it("a step-less round before the plan folds into the ONE plan group - never a ghost", () => {
    // The lookup id and the miss-seed id must be the same id: a pre-plan model frame
    // used to seed a `"tail"` group while the lookup asked for `"plan"`, and every
    // later plan-targeted frame then minted a second group (replay never builds the
    // ghost - a straight lane-parity hole).
    const v = fold(
      initialView(detail()),
      ev({ type: "activity", kind: "model", phase: "start", label: "round 1" }),
      ev({ type: "task.planned", seq: 2, plan: { steps: [] } }),
    );
    const groups = exec(v)?.groups ?? [];
    expect(groups.map((g) => g.id)).toEqual(["plan"]);
    expect(groups[0]?.kind).toBe("plan");
    expect(groups[0]?.agent).toBe("planner");
  });

  it("phase facts settle BY ACTOR: the clarifier's end never closes the namer's row", () => {
    // The intake pair runs concurrently (one journalled step, two coroutines) - identity
    // is the row's tool, so ends land on their own actor's pending row, in place.
    const v = fold(
      initialView(detail()),
      ev({
        type: "activity",
        kind: "phase",
        phase: "start",
        tool: "clarifier",
        label: "Reviewing your request",
      }),
      ev({
        type: "activity",
        kind: "phase",
        phase: "start",
        tool: "namer",
        label: "Naming the task",
      }),
      ev({
        type: "activity",
        kind: "phase",
        phase: "end",
        tool: "clarifier",
        label: "Reviewed your request",
        duration_ms: 900,
      }),
    );
    const plan = exec(v)?.groups.find((g) => g.id === "plan");
    expect(plan?.kind).toBe("plan"); // the pre-plan mint wears the plan face, not a step ghost
    const row = (tool: string) => {
      const r = plan?.rows.find((x) => x.t === "tool" && x.tool === tool);
      return r?.t === "tool" ? r : undefined;
    };
    expect(row("clarifier")?.pending).toBe(false);
    expect(row("clarifier")?.summary).toBe("Reviewed your request");
    expect(row("clarifier")?.durationMs).toBe(900);
    expect(row("namer")?.pending).toBe(true); // the concurrent start stays honestly open
  });

  it("a gate is a WAIT row: pending while parked, settled with the human's time", () => {
    const v = fold(
      initialView(detail()),
      stepRunning(2),
      ev({
        type: "gate.opened",
        seq: 3,
        step_id: "s1",
        gate_id: "g1",
        title: "One question before I continue",
        at: "2026-08-07T10:00:00Z",
        questions: {
          title: "One question before I continue",
          questions: [
            {
              id: "q1",
              prompt: "Which currency?",
              type: "single_select",
              options: [],
            },
          ],
        },
      }),
      ev({
        type: "gate.resolved",
        seq: 4,
        step_id: "s1",
        gate_id: "g1",
        action: "accept",
        content: { q1: "eur" },
        at: "2026-08-07T10:02:10Z",
      }),
    );
    const item = v.feed.find((i) => i.kind === "execution");
    const g = item?.kind === "execution" ? item.groups.find((x) => x.id === "s1") : undefined;
    const wait = g?.rows.find((r) => r.t === "tool" && r.tool === "gate");
    expect(wait?.t === "tool" && wait.pending).toBe(false);
    expect(wait?.t === "tool" && wait.summary).toBe("You answered");
    expect(wait?.t === "tool" && wait.durationMs).toBe(130_000); // 2m 10s of human time, on the record
    // The QA card still lands top-level - the thread's record is untouched.
    expect(v.feed.some((i) => i.kind === "qa")).toBe(true);
  });

  it("the per-step ledger accumulates calls · tokens · cost on the group", () => {
    const v = fold(
      initialView(detail()),
      stepRunning(2),
      ev({
        type: "usage.recorded",
        seq: 3,
        step_id: "s1",
        model: "claude-haiku-4-5",
        cost_usd: "0.010",
        units: { request: 1, input_token: 2000, output_token: 500 },
      }),
      ev({
        type: "usage.recorded",
        seq: 4,
        step_id: "s1",
        model: "claude-haiku-4-5",
        cost_usd: "0.008",
        units: { request: 1, input_token: 1500, output_token: 300 },
      }),
    );
    const item = v.feed.find((i) => i.kind === "execution");
    const g = item?.kind === "execution" ? item.groups.find((x) => x.id === "s1") : undefined;
    expect(g?.calls).toBe(2);
    expect(g?.tokens).toBe(4300);
    expect(g?.costUsd).toBeCloseTo(0.018);
  });

  it("the late receipt lands INSIDE the settled tree's producing group - no new item", () => {
    const v = fold(
      initialView(detail()),
      ev({
        type: "step.state",
        seq: 2,
        step_id: "s1",
        title: "Write",
        state: "done",
        attempt: 1,
        agent: "writer",
        detail: "",
      }),
      ev({ type: "task.delivered", seq: 3, summary: "done" }),
      ev({
        type: "verification.signed",
        seq: 4,
        step_id: "s1",
        artifact_id: "a1",
        verdict: "pass",
        suite: "document-suite",
        verifier_family: "google",
      }),
    );
    const items = v.feed.filter((i) => i.kind === "execution");
    expect(items).toHaveLength(1); // settled tree, no stray second item
    const g =
      items[0]?.kind === "execution" ? items[0].groups.find((x) => x.id === "s1") : undefined;
    const receipt = g?.rows.find((r) => r.t === "note");
    expect(receipt?.t === "note" && receipt.text).toBe(
      "Verified · document-suite · judged by google",
    );
    expect(receipt?.t === "note" && receipt.tone).toBe("success");
  });

  it("a post-settlement activity straggler is dropped, never a fresh tree", () => {
    const v = fold(
      initialView(detail()),
      ev({
        type: "step.state",
        seq: 2,
        step_id: "s1",
        title: "Write",
        state: "done",
        attempt: 1,
        agent: "writer",
        detail: "",
      }),
      ev({ type: "task.delivered", seq: 3, summary: "done" }),
      ev({
        type: "activity",
        kind: "tool",
        phase: "start",
        step_id: "s1",
        tool: "frame.read_file",
        tool_call_id: "late1",
      }),
    );
    expect(v.feed.filter((i) => i.kind === "execution")).toHaveLength(1);
    const g =
      v.feed[0]?.kind === "execution" ? v.feed[0].groups.find((x) => x.id === "s1") : undefined;
    expect(g?.rows.some((r) => r.id === "late1")).toBe(false);
  });

  it("a NEW round never eats the reasoning already streamed - shown text stays", () => {
    const step = stepRunning(2);
    const v = fold(
      initialView(detail()),
      step,
      ev({ type: "activity", kind: "model", phase: "start", step_id: "s1" }),
      ev({
        type: "activity",
        kind: "thinking",
        phase: "delta",
        step_id: "s1",
        text: "Outline first…",
      }),
      ev({
        type: "activity",
        kind: "model",
        phase: "end",
        step_id: "s1",
        duration_ms: 900,
      }),
      ev({ type: "activity", kind: "model", phase: "start", step_id: "s1" }), // round 2 opens
    );
    const item = v.feed.find((i) => i.kind === "execution");
    const g = item?.kind === "execution" ? item.groups.find((x) => x.id === "s1") : undefined;
    const round = g?.rows.find((r) => r.t === "round");
    expect(round?.t === "round" && round.pending).toBe(true); // thinking again…
    expect(round?.t === "round" && round.thinkingText).toBe("Outline first…"); // …but nothing vanished
  });

  it("usage.recorded stamps the step group's model attribution, first frame wins", () => {
    const v = fold(
      initialView(detail()),
      stepRunning(2),
      ev({
        type: "usage.recorded",
        seq: 3,
        step_id: "s1",
        model: "claude-haiku-4-5",
        cost_usd: "0.01",
      }),
      ev({
        type: "usage.recorded",
        seq: 4,
        step_id: "s1",
        model: "gemini-flash-latest",
        cost_usd: "0.01",
      }),
    );
    expect(exec(v)?.groups.find((g) => g.id === "s1")?.modelRef).toBe("claude-haiku-4-5");
    expect(v.usageCalls).toBe(2); // the ledger fold is untouched
  });
});

describe("lane parity - a refresh must not rearrange the transcript (2026-07-30)", () => {
  // The real shape from the wire: live carries message.* and artifact deltas that replay
  // does not; the durable record writes step.narration AFTER artifact.produced. Both lanes
  // must fold to the same items in the same order - the observed bug was the artifact card
  // sitting after the answer live and before it after refresh.
  const NARRATION =
    "I'll write the explainer now.\n\nDone. Verification: three H2 sections, 503 words.";
  const durable: TaskEvent[] = [
    ev({ type: "task.created", seq: 1, goal: "Explain consistent hashing." }),
    ev({
      type: "task.planned",
      seq: 2,
      plan: { steps: [{ id: "s1", title: "Write the doc", state: "pending" }] },
    }),
    ev({
      type: "tool.called",
      seq: 3,
      tool: "frame.write_file",
      summary: "/work/d.md",
      ok: true,
      tool_call_id: "tc1", // the reconcile key - the live activity start lands on THIS row
    }),
    ev({
      type: "artifact.snapshot",
      seq: 4,
      path: "/work/d.md",
      offset: 0,
      content: "# Consistent hashing…",
    }),
    ev({
      type: "artifact.produced",
      seq: 5,
      artifact_id: "a1",
      title: "The Doc",
      artifact_type: "document",
    }),
    ev({
      type: "verification.signed",
      seq: 6,
      verdict: "pass",
      suite: "doc@1",
      signature: "sig",
    }),
    ev({ type: "step.narration", seq: 7, markdown: NARRATION }),
    ev({ type: "task.delivered", seq: 8, summary: NARRATION }),
  ];
  // The live lane additionally carries the EPHEMERAL richness - phase facts, a model
  // round, the activity tool start - none of which replays; the parity pin below names
  // exactly that as the sanctioned delta.
  const live: TaskEvent[] = [
    durable[0],
    ev({
      type: "activity",
      kind: "phase",
      phase: "start",
      tool: "planner",
      label: "Drafting the plan",
    }),
    durable[1],
    ev({ type: "activity", kind: "model", phase: "start", label: "round 1" }),
    ev({ type: "message.start", agent: "writer" }),
    ev({ type: "message.delta", text: "I'll write the explainer now." }),
    ev({
      type: "activity",
      kind: "tool",
      phase: "start",
      tool: "frame.write_file",
      tool_call_id: "tc1",
      label: "/work/d.md",
    }),
    durable[2],
    ev({
      type: "artifact.delta",
      path: "/work/d.md",
      offset: 0,
      text: "# Consistent hashing…",
    }),
    durable[3],
    ev({
      type: "message.delta",
      text: "\n\nDone. Verification: three H2 sections, 503 words.",
    }),
    ev({ type: "message.end" }),
    durable[4],
    durable[5],
    durable[6],
    durable[7],
  ];

  const shape = (v: TaskView) =>
    v.feed.map((i) =>
      i.kind === "marker" ? `marker:${i.glyph}` : i.kind === "segment" ? "segment" : i.kind,
    );

  it("replay (durable only) and live (all frames) fold to the same order", () => {
    const replayed = fold(initialView(detail({ status: "delivered" })), ...durable);
    const streamed = fold(initialView(detail()), ...live);
    expect(shape(replayed)).toEqual(shape(streamed));
    expect(shape(replayed)).toEqual([
      "user",
      "execution", // the tree replaced the "Plan ready" marker
      "segment", // the answer BEFORE its artifact - where message.start sat live
      "artifact",
      "marker:verified", // pre-delivery receipt (fleet lane) keeps its quiet marker
      "epilogue", // the delivered stamp became the quiet end-of-turn block
    ]);
  });

  it("both lanes grow the IDENTICAL tree minus the live-only rows (the tree-parity law)", () => {
    // The sanctioned delta, NAMED (DESIGN.md): model-round rows, machinery rows
    // (engine/phase facts), pending flags and thinking text exist only as ephemeral
    // `activity` frames - a refresh drops those row classes and nothing else. Tool rows
    // (reconciled by tool_call_id), checklists, states and notes must be identical.
    // (`context.assembled` is the one DURABLE machinery row; it is deliberately absent
    // from this fixture - exempt its row id before stripping if it ever joins.)
    const MACHINERY = new Set(["engine", "phase", "clarifier", "planner", "namer"]);
    const treeShape = (v: TaskView) =>
      v.feed
        .filter((i) => i.kind === "execution")
        .map((i) =>
          i.kind === "execution"
            ? {
                settled: i.settled,
                groups: i.groups.map((g) => ({
                  id: g.id,
                  kind: g.kind,
                  state: g.state,
                  rows: g.rows
                    .filter((r) => !(r.t === "round" || (r.t === "tool" && MACHINERY.has(r.tool))))
                    .map((r) =>
                      r.t === "tool"
                        ? { t: r.t, tool: r.tool, summary: r.summary, ok: r.ok }
                        : { t: r.t },
                    ),
                })),
              }
            : null,
        );
    const replayed = fold(initialView(detail({ status: "delivered" })), ...durable);
    const streamed = fold(initialView(detail()), ...live);
    expect(treeShape(streamed)).toEqual(treeShape(replayed));
    // …and the delta was really exercised: the live lane DID carry the stripped classes,
    // so this pin can never go vacuously green again.
    const liveRows = (exec(streamed)?.groups ?? []).flatMap((g) => g.rows);
    expect(liveRows.some((r) => r.t === "round")).toBe(true);
    expect(liveRows.some((r) => r.t === "tool" && MACHINERY.has(r.tool))).toBe(true);
  });

  it("both lanes carry the same answer text and no stranded draft", () => {
    const replayed = fold(initialView(detail({ status: "delivered" })), ...durable);
    const streamed = fold(initialView(detail()), ...live);
    const text = (v: TaskView) =>
      v.feed.filter((i) => i.kind === "segment").map((i) => (i as { markdown: string }).markdown);
    expect(text(replayed)).toEqual(text(streamed));
    expect(replayed.draft).toBeNull();
    expect(streamed.draft).toBeNull();
  });
});

// ── the gate's thread record ─────────────────────────────────────────────────
// A resolved gate must LEAVE something behind: the qa card pairing each prompt with the
// answer as the user chose it (option LABELS, never wire values). Before this, an
// answered batch simply vanished from the transcript.

const GATE_OPENED = ev({
  type: "gate.opened",
  seq: 10,
  gate_id: "g1",
  kind: "input",
  title: "A few questions",
  questions: {
    title: "A few questions",
    questions: [
      {
        id: "runtime",
        prompt: "Where will this run?",
        type: "single_select",
        required: true,
        allow_other: true,
        options: [
          { value: "fc", label: "Existing Firecracker infra" },
          { value: "kvm", label: "Bare-metal Linux w/ KVM" },
        ],
      },
      {
        id: "notes",
        prompt: "Anything else?",
        type: "text",
        required: false,
        allow_other: false,
        options: [],
      },
    ],
  },
});

describe("the gate's thread record", () => {
  it("mints a qa card from accepted answers, wearing option LABELS", () => {
    const resolved = fold(
      initialView(detail()),
      GATE_OPENED,
      ev({
        type: "gate.resolved",
        seq: 11,
        gate_id: "g1",
        action: "accept",
        content: { runtime: "kvm", notes: "prefer Go" },
      }),
    );
    expect(resolved.gates).toHaveLength(0);
    const qa = resolved.feed.find((f) => f.kind === "qa");
    expect(qa && qa.kind === "qa" ? qa.items : []).toEqual([
      { prompt: "Where will this run?", answer: "Bare-metal Linux w/ KVM" },
      { prompt: "Anything else?", answer: "prefer Go" },
    ]);
  });

  it("a decline settles the tree's wait row and NAMES the skipped questions - no loose marker", () => {
    // Four intake rounds used to stack eight loose amber lines over a settled answer.
    // The record now lives in the tree, folded, and says WHAT was skipped.
    const resolved = fold(
      initialView(detail()),
      GATE_OPENED,
      ev({ type: "gate.resolved", seq: 11, gate_id: "g1", action: "decline" }),
    );
    expect(resolved.gates).toHaveLength(0);
    expect(resolved.feed.some((f) => f.kind === "qa")).toBe(false);
    expect(resolved.feed.some((f) => f.kind === "marker")).toBe(false);
    const item = resolved.feed.find((f) => f.kind === "execution");
    expect(item?.kind).toBe("execution");
    if (item?.kind !== "execution") return;
    const rows = item.groups.flatMap((g) => g.rows);
    const wait = rows.find((r) => r.t === "tool" && r.tool === "gate");
    expect(wait?.t === "tool" && wait.summary).toBe(
      "Skipped 2 questions - proceeding with defaults",
    );
    const note = rows.find((r) => r.t === "note" && r.id === "skip-g1");
    expect(note?.t === "note" && note.text).toBe("Skipped: Where will this run? · Anything else?");
  });

  it("a skipped question shows as skipped with its default, never under the answers", () => {
    // Skip on the last question accepts with the drafts; the server fills the rest from
    // defaults and names them (`defaulted`) - the person never chose those.
    for (const action of ["accept", "decline"] as const) {
      const resolved = fold(
        initialView(detail()),
        GATE_OPENED,
        ev({
          type: "gate.resolved",
          seq: 11,
          gate_id: "g1",
          action,
          content: { runtime: "kvm", notes: "none" },
          defaulted: ["notes"],
        }),
      );
      const qa = resolved.feed.find((f) => f.kind === "qa");
      expect(qa && qa.kind === "qa" ? qa.items : []).toEqual([
        { prompt: "Where will this run?", answer: "Bare-metal Linux w/ KVM" },
      ]);
      const item = resolved.feed.find((f) => f.kind === "execution");
      const rows = item?.kind === "execution" ? item.groups.flatMap((g) => g.rows) : [];
      const note = rows.find((r) => r.t === "note" && r.id === "skip-g1");
      expect(note?.t === "note" && note.text).toBe("Skipped: Anything else?");
    }
  });

  it("gate.opened parks WITHOUT a loose marker - the dock and the tree carry it", () => {
    const opened = fold(initialView(detail()), GATE_OPENED);
    expect(opened.status).toBe("waiting");
    expect(opened.feed.some((f) => f.kind === "marker")).toBe(false);
    const item = opened.feed.find((f) => f.kind === "execution");
    const rows = item?.kind === "execution" ? item.groups.flatMap((g) => g.rows) : [];
    const wait = rows.find((r) => r.t === "tool" && r.tool === "gate");
    expect(wait?.t === "tool" && wait.pending).toBe(true);
  });
});

describe("the minted title reaches list chrome", () => {
  it("task.renamed lands on view.title - the sidebar's window patch reads it", () => {
    const v = fold(
      initialView(detail()),
      ev({ type: "task.renamed", seq: 5, title: "The Q3 memo" }),
    );
    expect(v.title).toBe("The Q3 memo");
  });
});

describe("task.continued - the follow-up continues the SAME task (§8.4 v2)", () => {
  const settled = (): TaskView =>
    fold(
      initialView(detail({ status: "delivered", turn: 1 })),
      ev({ type: "task.status", seq: 8, status: "delivered" }),
    );

  it("folds the user's next turn: bubble + planning + turn, plan rolled into history", () => {
    const withPlan: TaskView = {
      ...settled(),
      steps: [{ id: "s1", title: "write", state: "done", attempt: 1 }],
    };
    const v = reduce(
      withPlan,
      ev({
        type: "task.continued",
        seq: 9,
        message: "now shorten it",
        turn: 2,
      }),
    );
    expect(v.status).toBe("planning");
    expect(v.turn).toBe(2);
    expect(v.delivered).toBe(false);
    expect(v.deliveredSummary).toBeUndefined();
    expect(v.steps).toEqual([]); // the new turn's task.planned/step.state build the next plan
    expect(v.planHistory).toHaveLength(1);
    const last = v.feed.at(-1);
    expect(last?.kind).toBe("user");
    expect((last as { text: string }).text).toBe("now shorten it");
  });

  it("reconciles the optimistic bubble by its send token - same id, pending cleared, ONE bubble", () => {
    const opt = addOptimisticUser(settled(), "now shorten it", "tok-1");
    const before = opt.feed.at(-1) as { id: string; pending?: boolean };
    expect(before.pending).toBe(true);
    const v = reduce(
      opt,
      ev({
        type: "task.continued",
        seq: 9,
        message: "now shorten it",
        turn: 2,
        client_token: "tok-1",
      }),
    );
    const bubbles = v.feed.filter((f) => f.kind === "user");
    expect(bubbles).toHaveLength(1);
    const after = v.feed.at(-1) as { id: string; pending?: boolean };
    expect(after.id).toBe(before.id); // the bubble never remounts
    expect(after.pending).toBeUndefined();
  });

  it("reconciles by text when the frame carries no token; a foreign message appends", () => {
    const opt = addOptimisticUser(settled(), "now  shorten it", "tok-2");
    const matched = reduce(
      opt,
      ev({
        type: "task.continued",
        seq: 9,
        message: "now shorten it",
        turn: 2,
      }),
    );
    expect(matched.feed.filter((f) => f.kind === "user")).toHaveLength(1);

    const foreign = reduce(
      opt,
      ev({
        type: "task.continued",
        seq: 9,
        message: "translate it instead",
        turn: 2,
      }),
    );
    expect(foreign.feed.filter((f) => f.kind === "user")).toHaveLength(2);
  });

  it("a replayed task.continued frame seq-dedups to the same reference", () => {
    const frame = ev({
      type: "task.continued",
      seq: 9,
      message: "again",
      turn: 2,
    });
    const once = reduce(settled(), frame);
    expect(reduce(once, frame)).toBe(once);
  });

  it("removeOptimistic takes a failed send's bubble back off", () => {
    const opt = addOptimisticUser(settled(), "never landed", "tok-3");
    const v = removeOptimistic(opt, "tok-3");
    expect(v.feed.filter((f) => f.kind === "user")).toHaveLength(0);
  });

  it("the next turn's stream builds a fresh current plan after the reset", () => {
    const v = fold(
      settled(),
      ev({ type: "task.continued", seq: 9, message: "next", turn: 2 }),
      ev({ type: "task.status", seq: 10, status: "running" }),
      ev({
        type: "step.state",
        seq: 11,
        step_id: "s2",
        title: "shorten",
        state: "running",
        attempt: 1,
        detail: "",
      }),
    );
    expect(v.status).toBe("running");
    expect(v.steps.map((s) => s.id)).toEqual(["s2"]);
  });
});

describe("task.feedback - the thumbs (ADR-019)", () => {
  it("folds the newest rating and resets when the next turn opens", () => {
    const rated = fold(
      initialView(detail({ status: "delivered", turn: 1 })),
      ev({ type: "task.feedback", seq: 9, rating: "down", turn: 1 }),
      ev({ type: "task.feedback", seq: 10, rating: "up", turn: 1 }),
    );
    expect(rated.feedback).toBe("up"); // a changed mind: the newest frame wins

    const next = reduce(rated, ev({ type: "task.continued", seq: 11, message: "again", turn: 2 }));
    expect(next.feedback).toBeNull(); // a new turn grades fresh
  });

  it("a replayed feedback frame seq-dedups to the same reference", () => {
    const frame = ev({ type: "task.feedback", seq: 9, rating: "up", turn: 1 });
    const once = reduce(initialView(detail({ status: "delivered" })), frame);
    expect(reduce(once, frame)).toBe(once);
  });
});

describe("settlement closes the dock (ADR-019 - a question never walls off an answer)", () => {
  const OPEN_GATE = ev({
    type: "gate.opened",
    seq: 5,
    gate_id: "g9",
    title: "One question before I continue",
    questions: {
      title: "One question before I continue",
      questions: [{ id: "q1", prompt: "Which region?", type: "text" }],
    },
  });

  it("task.delivered clears any surviving gates", () => {
    const v = fold(
      initialView(detail()),
      OPEN_GATE,
      ev({ type: "task.delivered", seq: 6, summary: "done" }),
    );
    expect(v.gates).toHaveLength(0);
    expect(v.status).toBe("delivered");
  });

  it("task.cancelled clears them too", () => {
    const v = fold(initialView(detail()), OPEN_GATE, ev({ type: "task.cancelled", seq: 6 }));
    expect(v.gates).toHaveLength(0);
  });
});

describe("gatesFor - a viewer is offered only the scopes the server takes from them (ADR-062)", () => {
  // `gate.opened` carries the gate's whole scope list to every viewer of the task; `always`
  // is the creator's alone (`gates.resolve` 403s anyone else).
  const parked = fold(
    initialView(detail()),
    ev({
      type: "task.created",
      seq: 1,
      goal: "Update the orders",
      actor: { type: "user", id: "ada", name: "Ada" },
    }),
    ev({
      type: "gate.opened",
      seq: 2,
      gate_id: "g1",
      step_id: "s1",
      kind: "approve",
      source: "browser",
      host: "vendor.example.com",
      scopes: ["once", "task", "always"],
      title: "Open vendor.example.com?",
    }),
  );

  it("the task's creator keeps always", () => {
    expect(gatesFor(parked, "ada")[0].scopes).toEqual(["once", "task", "always"]);
  });

  it("a Project editor - or a viewer the page cannot place - is offered once and task only", () => {
    expect(gatesFor(parked, "bob")[0].scopes).toEqual(["once", "task"]);
    expect(gatesFor(parked, undefined)[0].scopes).toEqual(["once", "task"]);
  });

  it("no creator on the record yet means no always for anyone", () => {
    const unplaced = fold(
      initialView(detail()),
      ev({
        type: "gate.opened",
        seq: 2,
        gate_id: "g1",
        kind: "approve",
        scopes: ["once", "task", "always"],
      }),
    );
    expect(gatesFor(unplaced, "ada")[0].scopes).toEqual(["once", "task"]);
  });

  it("a gate offering no scopes (a danger ask, a connector approval) passes through as it is", () => {
    const danger = fold(
      parked,
      ev({ type: "gate.resolved", seq: 3, gate_id: "g1", action: "accept" }),
      ev({
        type: "gate.opened",
        seq: 4,
        gate_id: "g2",
        kind: "approve",
        source: "browser",
        scopes: [],
      }),
    );
    expect(gatesFor(danger, "bob")).toEqual(danger.gates);
  });
});

describe("artifact.revised across a turn boundary (ADR-028)", () => {
  it("a revision with no card to repoint becomes the turn's card", () => {
    const v = fold(
      initialView(detail()),
      ev({
        type: "artifact.revised",
        seq: 3,
        artifact_id: "new-face",
        supersedes_id: "prior-turn-id",
        title: "The Lighthouse Economy",
        artifact_type: "document",
        filename: "lighthouse-economy.docx",
        revision: 3,
      }),
    );
    expect(v.artifacts.some((a) => a.id === "new-face")).toBe(true);
    expect(v.feed.some((f) => f.kind === "artifact" && f.artifactId === "new-face")).toBe(true);
  });

  it("a restore-shaped revised (known supersedes) repoints in place - no new card (ADR-033)", () => {
    // A restore emits a PLAIN artifact.revised whose supersedes_id is the current head:
    // the shelf entry swaps its id (same position), the feed card repoints, nothing mints.
    let v = fold(
      initialView(detail()),
      ev({
        type: "artifact.produced",
        seq: 2,
        artifact_id: "head-v2",
        title: "Notes",
        artifact_type: "document",
      }),
    );
    const cards = v.feed.filter((f) => f.kind === "artifact").length;
    v = fold(
      v,
      ev({
        type: "artifact.revised",
        seq: 3,
        artifact_id: "restored-v3",
        supersedes_id: "head-v2",
        title: "Notes",
        artifact_type: "document",
        revision: 3,
      }),
    );
    expect(v.artifacts.some((a) => a.id === "restored-v3")).toBe(true);
    expect(v.artifacts.some((a) => a.id === "head-v2")).toBe(false);
    expect(v.feed.filter((f) => f.kind === "artifact").length).toBe(cards);
    expect(v.feed.some((f) => f.kind === "artifact" && f.artifactId === "restored-v3")).toBe(true);
  });
});

describe("talk until it is work (ADR-058) - the route and the proposal card", () => {
  it("task.planned carries the route onto the view and the epilogue wears it", () => {
    const v = fold(
      initialView(detail()),
      ev({
        type: "task.planned",
        seq: 2,
        plan: { steps: [] },
        route: "express",
        route_source: "anchor",
        route_why: "unsure whether this is talk or work",
      }),
      ev({
        type: "task.delivered",
        seq: 3,
        summary: "He found the first exact solution.",
      }),
    );
    expect(v.route).toEqual({
      lane: "express",
      source: "anchor",
      why: "unsure whether this is talk or work",
    });
    const ep = v.feed.find((i) => i.kind === "epilogue");
    expect(ep?.kind === "epilogue" && ep.route?.source).toBe("anchor");
  });

  it("a hatch re-plan overwrites the turn's route; the next turn resets it", () => {
    const planned = fold(
      initialView(detail()),
      ev({
        type: "task.planned",
        seq: 2,
        plan: { steps: [] },
        route: "express",
        route_source: "anchor",
      }),
      ev({
        type: "task.planned",
        seq: 3,
        plan: { steps: [] },
        route: "standard",
        route_source: "hatch",
        route_why: "the answerer escalated: it wants sources",
      }),
    );
    expect(planned.route?.source).toBe("hatch");
    const next = fold(planned, ev({ type: "task.continued", seq: 4, message: "close", turn: 2 }));
    expect(next.route).toBeNull();
  });

  it("task.proposed mints an open card; the next turn closes it", () => {
    const v = fold(
      initialView(detail()),
      ev({
        type: "task.proposed",
        seq: 2,
        goal: "A one-page biography with sources",
        deliverable: "document",
        why: "sources",
        turn: 1,
      }),
    );
    const card = v.feed.find((i) => i.kind === "proposal");
    expect(card?.kind === "proposal" && card.open && card.deliverable).toBe("document");
    const next = fold(v, ev({ type: "task.continued", seq: 3, message: "thanks", turn: 2 }));
    const closed = next.feed.find((i) => i.kind === "proposal");
    expect(closed?.kind === "proposal" && closed.open).toBe(false);
  });
});

// Multiplayer: the record names every human action, and the timeline says
// who. Events that carry no actor (a worker's, or any row from before the phase) fold exactly
// as they always did - personal tasks look the same.
describe("who did it - authors and control lines (ADR-062)", () => {
  const ADA = { type: "user", id: "ada-id", name: "Ada" };
  const BOB = { type: "user", id: "bob-id", name: "Bob" };
  const CY = { type: "user", id: "cy-id", name: "Cy" };

  const users = (v: TaskView) =>
    v.feed.filter((i) => i.kind === "user") as Extract<
      TaskView["feed"][number],
      { kind: "user" }
    >[];
  const controls = (v: TaskView) => v.feed.flatMap((i) => (i.kind === "control" ? [i.text] : []));

  it("task.created carries its actor onto the opening ask", () => {
    const v = fold(
      initialView(detail()),
      ev({ type: "task.created", seq: 1, goal: "write a memo", actor: ADA }),
    );
    expect(users(v)[0].author).toEqual({ id: "ada-id", name: "Ada" });
    expect(users(v)[0].opening).toBe(true);
  });

  it("task.continued: a frame from another window appends with its author; ours reconciles and gains it", () => {
    const base = fold(
      initialView(detail()),
      ev({ type: "task.created", seq: 1, goal: "a", actor: ADA }),
    );
    const other = fold(
      base,
      ev({
        type: "task.continued",
        seq: 2,
        message: "and b",
        turn: 2,
        actor: BOB,
      }),
    );
    expect(users(other)[1].author).toEqual({ id: "bob-id", name: "Bob" });

    const painted = addOptimisticUser(base, "and c", "tok");
    expect(users(painted)[1].author).toBeUndefined(); // the optimistic row is yours: no name yet
    const settled = fold(
      painted,
      ev({
        type: "task.continued",
        seq: 3,
        message: "and c",
        turn: 2,
        client_token: "tok",
        actor: ADA,
      }),
    );
    expect(users(settled)).toHaveLength(2); // reconciled in place, never duplicated
    expect(users(settled)[1].id).toBe("opt-tok");
    expect(users(settled)[1].author).toEqual({ id: "ada-id", name: "Ada" });
    expect(users(settled)[1].pending).toBeUndefined();
  });

  it("plan.edited{steer} carries its actor, and reads as a steer", () => {
    const v = fold(
      initialView(detail({ status: "running" })),
      ev({
        type: "plan.edited",
        seq: 2,
        edit: { kind: "steer", reason: "Focus on pricing" },
        actor: BOB,
      }),
    );
    expect(users(v)[0]).toMatchObject({
      text: "Focus on pricing",
      author: { id: "bob-id", name: "Bob" },
      steered: true,
    });
  });

  it("without an actor there is no author and no control line - personal tasks are unchanged", () => {
    const v = fold(
      initialView(detail()),
      ev({ type: "task.created", seq: 1, goal: "a" }),
      ev({ type: "task.status", seq: 2, status: "running" }),
      ev({ type: "task.status", seq: 3, status: "paused" }),
      ev({ type: "task.status", seq: 4, status: "running" }),
      ev({ type: "task.renamed", seq: 5, title: "New name" }),
      ev({
        type: "plan.edited",
        seq: 6,
        edit: { kind: "steer", reason: "shorter" },
      }),
      ev({ type: "task.status", seq: 7, status: "cancelled" }),
    );
    expect(controls(v)).toEqual([]);
    expect(users(v).every((u) => u.author === undefined)).toBe(true);
    expect(users(v).some((u) => u.steered)).toBe(true); // a steer is still a steer
  });

  it("task.status{paused} names who paused; the next running names who resumed", () => {
    const v = fold(
      initialView(detail({ status: "running" })),
      ev({ type: "task.status", seq: 2, status: "paused", actor: ADA }),
      ev({ type: "task.status", seq: 3, status: "running", actor: BOB }),
    );
    expect(controls(v)).toEqual(["Ada paused", "Bob resumed"]);
    expect(v.status).toBe("running");
  });

  it("a running frame that does not follow a pause is not a resume", () => {
    const v = fold(
      initialView(detail({ status: "planning" })),
      ev({ type: "task.status", seq: 2, status: "running", actor: ADA }),
    );
    expect(controls(v)).toEqual([]);
  });

  it("task.status{cancelled} reads as 'cancelled the task' - and the gates it closes add nothing", () => {
    const opened = fold(initialView(detail({ status: "running" })), GATE_OPENED);
    const v = fold(
      opened,
      ev({ type: "task.status", seq: 11, status: "cancelled", actor: BOB }),
      ev({
        type: "gate.resolved",
        seq: 12,
        gate_id: "g1",
        action: "cancel",
        actor: BOB,
      }),
    );
    expect(controls(v)).toEqual(["Bob cancelled the task"]);
    expect(v.status).toBe("cancelled");
  });

  it("task.renamed names who renamed it, and the title still lands", () => {
    const v = fold(
      initialView(detail()),
      ev({
        type: "task.renamed",
        seq: 2,
        title: "Launch comparison",
        actor: ADA,
      }),
    );
    expect(controls(v)).toEqual(["Ada renamed the task"]);
    expect(v.title).toBe("Launch comparison");
  });

  describe("budget gates (B3)", () => {
    it("dockFor gives a scoped budget gate its own face, and the cost confirmation none", () => {
      expect(dockFor({ kind: "budget", questions: [], budget_scope: "task" })).toBe("budget");
      expect(dockFor({ kind: "budget", questions: [], budget_scope: "project" })).toBe("budget");
      expect(dockFor({ kind: "budget", questions: [], budget_scope: "" })).toBe("none");
      expect(dockFor({ kind: "budget", questions: [] })).toBe("none");
    });
    it("a budget change reads as a control line naming who, which way and how much", () => {
      const v = fold(
        initialView(detail()),
        ev({ type: "task.budget_set", seq: 2, usd: "2.00", previous_usd: "0.01", actor: BOB }),
        ev({ type: "task.budget_set", seq: 3, usd: "1.00", previous_usd: "2.00", actor: BOB }),
        ev({ type: "task.budget_set", seq: 4, usd: "3.00", previous_usd: null, actor: ADA }),
      );
      expect(controls(v)).toEqual([
        "Bob raised the budget to $2.00",
        "Bob lowered the budget to $1.00",
        "Ada set the budget to $3.00",
      ]);
    });
    it("a budget gate carries the Bursar's figures into GateView, for the dock's raise", () => {
      const figures = {
        ceiling_usd: "10.00",
        spent_usd: "4.00",
        projected_usd: "8.00",
      };
      const opened = fold(
        initialView(detail({ status: "running" })),
        ev({
          type: "gate.opened",
          seq: 3,
          gate_id: "g1",
          kind: "budget",
          title: "Budget reached",
          reason: "$4.00 spent; the next wave needs about $8.00 against the task budget of $10.00",
          budget_scope: "task",
          budget_figures: figures,
        }),
      );
      expect(opened.gates[0].budget_figures).toEqual(figures);
    });
    it("a budget gate's wait row carries its reason, which is all a viewer sees", () => {
      const opened = fold(
        initialView(detail({ status: "running" })),
        ev({
          type: "gate.opened",
          seq: 3,
          gate_id: "g1",
          step_id: "s1",
          kind: "budget",
          title: "Budget reached",
          reason:
            "the task budget of $0.01 is spent ($0.02 so far); raise it and resume, or cancel",
          budget_scope: "task",
        }),
      );
      const waitRow = opened.feed
        .flatMap((f) => (f.kind === "execution" ? f.groups : []))
        .flatMap((g) => g.rows)
        .find((r) => r.t === "tool" && r.id === "gate-g1");
      expect(waitRow && "summary" in waitRow ? waitRow.summary : "").toBe(
        "Waiting on a budget - the task budget of $0.01 is spent ($0.02 so far); raise it and resume, or cancel",
      );
    });
  });

  it("gate.resolved: a question answered by Cy reads 'Cy answered', and the Q&A card stays", () => {
    const v = fold(
      initialView(detail({ status: "running" })),
      GATE_OPENED,
      ev({
        type: "gate.resolved",
        seq: 11,
        gate_id: "g1",
        action: "accept",
        content: { runtime: "fc" },
        actor: CY,
      }),
    );
    expect(controls(v)).toEqual(["Cy answered"]);
    expect(v.feed.some((i) => i.kind === "qa")).toBe(true);
  });

  it("gate.resolved: an approve gate reads 'approved' or 'declined'", () => {
    const approve = (seq: number, gate_id: string) =>
      ev({
        type: "gate.opened",
        seq,
        gate_id,
        kind: "approve",
        title: "Send the email",
      });
    const yes = fold(
      initialView(detail({ status: "running" })),
      approve(10, "g1"),
      ev({
        type: "gate.resolved",
        seq: 11,
        gate_id: "g1",
        action: "accept",
        actor: CY,
      }),
    );
    expect(controls(yes)).toEqual(["Cy approved"]);
    const no = fold(
      initialView(detail({ status: "running" })),
      approve(10, "g2"),
      ev({
        type: "gate.resolved",
        seq: 11,
        gate_id: "g2",
        action: "decline",
        actor: CY,
      }),
    );
    expect(controls(no)).toEqual(["Cy declined"]);
  });

  it("gate.resolved without an actor (a timeout, the clock) stays silent", () => {
    const v = fold(
      initialView(detail({ status: "running" })),
      GATE_OPENED,
      ev({ type: "gate.resolved", seq: 11, gate_id: "g1", action: "decline" }),
    );
    expect(controls(v)).toEqual([]);
  });

  it("control lines take the next id and never trip the phase law: machinery after one still joins its tree", () => {
    const v = fold(
      initialView(detail({ status: "running" })),
      stepRunning(2),
      ev({ type: "task.status", seq: 3, status: "paused", actor: ADA }),
      ev({ type: "task.status", seq: 4, status: "running", actor: ADA }),
      stepRunning(5, { state: "done" }),
    );
    expect(v.feed.map((i) => i.kind)).toEqual(["execution", "control", "control"]);
    expect(new Set(v.feed.map((i) => i.id)).size).toBe(3);
    // one tree, and the step that finished after both lines landed in IT
    expect(v.feed.filter((i) => i.kind === "execution")).toHaveLength(1);
    expect(exec(v)?.groups.find((g) => g.id === "s1")?.state).toBe("done");
  });

  it("a name is data the person controls: whitespace collapses, length caps at 60", () => {
    const long = "Ada\n\nLovelace ".repeat(20);
    const v = fold(
      initialView(detail()),
      ev({
        type: "task.renamed",
        seq: 2,
        title: "x",
        actor: { type: "user", id: "a", name: long },
      }),
    );
    const line = controls(v)[0];
    expect(line).toMatch(/^Ada Lovelace/);
    expect(line).not.toContain("\n");
    expect(line.replace(" renamed the task", "").length).toBeLessThanOrEqual(60);
  });
});

describe("steer acknowledgements (ADR-064)", () => {
  const BOB = { type: "user", id: "bob-id", name: "Bob" };
  const ADA = { type: "user", id: "ada-id", name: "Ada" };
  const users = (v: TaskView) =>
    v.feed.filter((i) => i.kind === "user") as Extract<
      TaskView["feed"][number],
      { kind: "user" }
    >[];
  const controls = (v: TaskView) => v.feed.flatMap((i) => (i.kind === "control" ? [i.text] : []));
  const steer = (seq: number, command_id: string, epoch: number) =>
    ev({
      type: "plan.edited",
      seq,
      actor: BOB,
      edit: { kind: "steer", reason: "use euros", command_id, epoch },
    });
  const toolRows = (v: TaskView) =>
    v.feed.flatMap((i) =>
      i.kind === "execution" ? i.groups.flatMap((g) => g.rows.filter((r) => r.t === "tool")) : [],
    ) as Extract<ExecRow, { t: "tool" }>[];

  it("a steer is accepted at once and the view learns its epoch", () => {
    const v = fold(initialView(detail({ status: "running", plan_epoch: 1 })), steer(2, "c1", 2));
    const bubble = users(v)[0];
    expect(bubble).toMatchObject({
      commandId: "c1",
      ack: { state: "accepted", line: "Accepted · applies at the next safe point" },
    });
    expect(v.steerEpoch).toBe(2);
  });

  it("the epoch starts from the detail and never moves back", () => {
    expect(initialView(detail({ plan_epoch: 4 })).steerEpoch).toBe(4);
    expect(initialView(detail()).steerEpoch).toBe(0);
    const v = fold(initialView(detail({ status: "running", plan_epoch: 5 })), steer(2, "c1", 3));
    expect(v.steerEpoch).toBe(5);
  });

  it("a new turn opens at its own epoch", () => {
    const v = fold(
      initialView(detail({ status: "delivered", plan_epoch: 2 })),
      ev({ type: "task.continued", seq: 3, message: "and now b", turn: 2, epoch: 3 }),
    );
    expect(v.steerEpoch).toBe(3);
  });

  it("applied names the step and the safe point", () => {
    const v = fold(
      initialView(detail({ status: "running" })),
      steer(2, "c1", 2),
      ev({
        type: "steer.applied",
        seq: 3,
        command_id: "c1",
        step_title: "Draft report",
        safe_point: "before_tool",
      }),
    );
    expect(users(v)[0].ack).toEqual({
      state: "applied",
      line: "Applied to Draft report before its next tool call",
    });
  });

  it("a steer taken after an outside action says what already happened", () => {
    const v = fold(
      initialView(detail({ status: "running" })),
      steer(2, "c1", 2),
      ev({
        type: "steer.applied",
        seq: 3,
        command_id: "c1",
        step_title: "Save two notes",
        safe_point: "before_tool",
        already_done: ["notesdemo_write_note"],
      }),
    );
    expect(users(v)[0].ack?.line).toBe(
      "Applied to Save two notes before its next tool call · Already done before your change: notesdemo write note - ask for a follow-up to correct it",
    );
  });

  it("each safe point has its own words, and a missing title reads the step", () => {
    const line = (safe_point: string, step_title?: string) =>
      users(
        fold(
          initialView(detail({ status: "running" })),
          steer(2, "c1", 2),
          ev({
            type: "steer.applied",
            seq: 3,
            command_id: "c1",
            safe_point,
            ...(step_title ? { step_title } : {}),
          }),
        ),
      )[0].ack?.line;
    expect(line("before_model", "Draft")).toBe("Applied to Draft before its next model call");
    expect(line("final_answer", "Draft")).toBe("Applied to Draft before it finished");
    expect(line("approval", "Draft")).toBe("Superseded the approval Draft was waiting on");
    expect(line("replan")).toBe("Re-planned the remaining steps");
    expect(line("interrupt")).toBe("Cut a reply short");
    expect(line("before_tool")).toBe("Applied to the step before its next tool call");
    expect(line("rerun")).toBe("The step is going again with it");
  });

  it("an acknowledgement finds its bubble by command id, and only that one", () => {
    const v = fold(
      initialView(detail({ status: "running" })),
      steer(2, "c1", 2),
      steer(3, "c2", 3),
      ev({
        type: "steer.applied",
        seq: 4,
        command_id: "c2",
        step_title: "Draft",
        safe_point: "before_model",
      }),
    );
    expect(users(v).map((u) => u.ack?.state)).toEqual(["accepted", "applied"]);
  });

  it("rejected and retracted say why", () => {
    const rejected = fold(
      initialView(detail({ status: "running" })),
      steer(2, "c1", 2),
      ev({
        type: "steer.rejected",
        seq: 3,
        command_id: "c1",
        reason: "the task was cancelled before this applied",
      }),
    );
    expect(users(rejected)[0].ack?.line).toBe("Not applied: the task was cancelled first");
    const retracted = fold(
      initialView(detail({ status: "running" })),
      steer(2, "c1", 2),
      ev({ type: "steer.retracted", seq: 3, command_id: "c1", actor: BOB }),
    );
    expect(users(retracted)[0].ack?.line).toBe("Retracted by Bob");
  });

  it("every rejection reason has its own words; an unknown one is shown as sent", () => {
    const line = (reason: string) =>
      users(
        fold(
          initialView(detail({ status: "running" })),
          steer(2, "c1", 2),
          ev({ type: "steer.rejected", seq: 3, command_id: "c1", reason }),
        ),
      )[0].ack;
    expect(line("the task failed before this applied")).toEqual({
      state: "rejected",
      line: "Not applied: the task failed first",
    });
    expect(line("the turn ended before this applied")?.line).toBe(
      "Not applied: the turn ended first",
    );
    expect(line("something new")?.line).toBe("Not applied: something new");
  });

  it("a pause reads Pausing until it takes hold", () => {
    const pausing = fold(
      initialView(detail({ status: "running" })),
      ev({ type: "task.status", seq: 2, status: "paused", actor: ADA }),
    );
    expect(pausing.pause).toEqual({ by: "Ada", applied: false });
    const paused = fold(pausing, ev({ type: "pause.applied", seq: 3, safe_point: "before_tool" }));
    expect(paused.pause).toEqual({ by: "Ada", applied: true });
    const resumed = fold(
      paused,
      ev({ type: "task.status", seq: 4, status: "running", actor: BOB }),
    );
    expect(resumed.pause).toBeNull();
  });

  it("an unattributed pause tracks nothing, and a stray pause.applied invents nothing", () => {
    const v = fold(
      initialView(detail({ status: "running" })),
      ev({ type: "task.status", seq: 2, status: "paused" }),
      ev({ type: "pause.applied", seq: 3, safe_point: "boundary" }),
    );
    expect(v.pause).toBeNull();
    expect(v.status).toBe("paused");
  });

  it("a rerun greys what it superseded, and remembers whose steer did it", () => {
    const v = fold(
      initialView(detail({ status: "running" })),
      steer(2, "c1", 2),
      ev({
        type: "steer.applied",
        seq: 3,
        command_id: "c1",
        step_title: "Draft",
        safe_point: "rerun",
        superseded: ["a1"],
        author: "Bob",
      }),
    );
    expect(v.superseded).toEqual({ a1: "Bob" });
    expect(users(v)[0].ack?.line).toBe("Draft is going again with it");
    const nameless = fold(
      initialView(detail({ status: "running" })),
      steer(2, "c1", 2),
      ev({
        type: "steer.applied",
        seq: 3,
        command_id: "c1",
        safe_point: "rerun",
        superseded: ["a1", "a2"],
      }),
    );
    expect(nameless.superseded).toEqual({ a1: "a teammate", a2: "a teammate" });
  });

  it("what it made leaves out what a re-run superseded", () => {
    const v = fold(
      initialView(detail({ status: "running" })),
      ev({
        type: "artifact.produced",
        seq: 2,
        artifact_id: "a1",
        title: "Report v1",
        artifact_type: "document",
      }),
      steer(3, "c1", 2),
      ev({
        type: "steer.applied",
        seq: 4,
        command_id: "c1",
        safe_point: "rerun",
        superseded: ["a1"],
        author: "Bob",
      }),
      ev({
        type: "artifact.produced",
        seq: 5,
        artifact_id: "a2",
        title: "Report v2",
        artifact_type: "document",
      }),
    );
    expect(deliveredArtifacts(v).map((a) => a.artifactId)).toEqual(["a2"]);
  });

  it("a re-run's identical republish is a NEW artifact: it lands on the shelf, the superseded one stays off", () => {
    // R52: superseded output is never taken back - the server mints a fresh row (a new id) and
    // announces it once; the old id stays superseded and is never re-sent.
    const produced = (seq: number, artifact_id: string) =>
      ev({
        type: "artifact.produced",
        seq,
        artifact_id,
        title: "Report",
        artifact_type: "document",
        role: "deliverable",
      });
    const v = fold(
      initialView(detail({ status: "running" })),
      produced(2, "a1"),
      steer(3, "c1", 2),
      ev({
        type: "steer.applied",
        seq: 4,
        command_id: "c1",
        safe_point: "rerun",
        superseded: ["a1"],
        author: "Bob",
      }),
      produced(5, "a2"),
    );
    expect(v.superseded).toEqual({ a1: "Bob" });
    expect(deliveredArtifacts(v).map((a) => a.artifactId)).toEqual(["a2"]);
    const shelf = v.artifacts.filter((a) => !Object.hasOwn(v.superseded, a.id));
    expect(shelf.map((a) => a.id)).toEqual(["a2"]);
    // the transcript keeps both: the greyed history card, then the live one
    expect(v.feed.flatMap((f) => (f.kind === "artifact" ? [f.artifactId] : []))).toEqual([
      "a1",
      "a2",
    ]);
  });

  it("a pause gate is nobody's question: no dock, no waiting, no 'answered' - the resume line tells it", () => {
    const v = fold(
      initialView(detail({ status: "running" })),
      stepRunning(2),
      ev({ type: "task.status", seq: 3, status: "paused", actor: ADA }),
      ev({ type: "pause.applied", seq: 4, safe_point: "before_tool", step_id: "s1" }),
      ev({
        type: "gate.opened",
        seq: 5,
        gate_id: "gp",
        kind: "pause",
        title: "Paused",
        step_id: "s1",
        at: "2026-10-02T10:00:00Z",
      }),
    );
    expect(v.gates).toEqual([]);
    expect(v.status).toBe("paused");
    const opened = toolRows(v).find((r) => r.id === "gate-gp");
    expect(opened).toMatchObject({
      tool: "pause",
      summary: "Paused before its next tool call",
      pending: false,
    });
    const resumed = fold(
      v,
      ev({
        type: "gate.resolved",
        seq: 6,
        gate_id: "gp",
        action: "accept",
        actor: BOB,
        at: "2026-10-02T10:02:00Z",
      }),
      ev({ type: "task.status", seq: 7, status: "running", actor: BOB }),
    );
    expect(controls(resumed)).toEqual(["Ada paused", "Bob resumed"]);
    const settled = toolRows(resumed).find((r) => r.id === "gate-gp");
    expect(settled).toMatchObject({
      tool: "pause",
      summary: "Paused before its next tool call",
      durationMs: 120_000,
    });
    expect(JSON.stringify(resumed.feed)).not.toMatch(/answered|Waiting on you/);
  });

  it("a step waiting for its device reads Waiting for its name until the resume lifts it", () => {
    // The device-away PAUSE gate wears its reason's prefix and its title.
    const waiting = fold(
      initialView(detail({ status: "running" })),
      stepRunning(2),
      // the server's order: the gate opens, then the task goes paused
      ev({
        type: "gate.opened",
        seq: 3,
        gate_id: "ga",
        kind: "pause",
        step_id: "s1",
        at: "2026-10-02T10:00:00Z",
        title: "Waiting for MacBook Pro",
        reason:
          "device_away:away\nPrismoid cannot reach MacBook Pro. The step waits; resume the task once it is back.",
      }),
      ev({ type: "task.status", seq: 4, status: "paused" }),
    );
    expect(waiting.deviceAway).toEqual({ gate_id: "ga", title: "Waiting for MacBook Pro" });
    expect(waiting.gates).toEqual([]);
    expect(toolRows(waiting).find((r) => r.id === "gate-ga")).toMatchObject({
      summary: "Waiting for MacBook Pro",
    });
    const resumed = fold(
      waiting,
      ev({
        type: "gate.resolved",
        seq: 5,
        gate_id: "ga",
        action: "accept",
        actor: ADA,
        at: "2026-10-02T10:05:00Z",
      }),
      ev({ type: "task.status", seq: 6, status: "running", actor: ADA }),
    );
    expect(resumed.deviceAway).toBeNull();
    // a person's own pause is not a device wait
    const paused = fold(
      initialView(detail({ status: "running" })),
      ev({ type: "task.status", seq: 2, status: "paused", actor: ADA }),
      ev({
        type: "gate.opened",
        seq: 3,
        gate_id: "gp",
        kind: "pause",
        title: "Paused",
        at: "2026-10-02T10:00:00Z",
      }),
    );
    expect(paused.deviceAway).toBeNull();
  });

  it("a page loaded while the step waits for its device reads it from the task read", () => {
    const loaded = initialView(
      detail({ status: "paused", waiting_for: "Waiting for MacBook Pro" }),
    );
    expect(loaded.deviceAway).toEqual({ gate_id: "", title: "Waiting for MacBook Pro" });
    expect(initialView(detail({ status: "running" })).deviceAway).toBeNull();
    // the replayed gate.opened pins the gate; its resolve and the resume lift the wait
    const pinned = fold(
      loaded,
      ev({
        type: "gate.opened",
        seq: 4,
        gate_id: "ga",
        kind: "pause",
        at: "2026-10-02T10:00:00Z",
        title: "Waiting for MacBook Pro",
        reason: "device_away:away\nPrismoid cannot reach MacBook Pro.",
      }),
    );
    expect(pinned.deviceAway).toEqual({ gate_id: "ga", title: "Waiting for MacBook Pro" });
    // a seed no replayed gate pinned is lifted by the task leaving paused
    const running = fold(
      loaded,
      ev({ type: "task.status", seq: 9, status: "running", actor: ADA }),
    );
    expect(running.deviceAway).toBeNull();
  });

  it("the creator's Done on a device hand-off reads as handing the browser back", () => {
    const v = fold(
      initialView(detail({ status: "running" })),
      stepRunning(2),
      ev({
        type: "gate.opened",
        seq: 3,
        gate_id: "gc",
        kind: "control",
        source: "handoff",
        title: 'The agent needs someone to "sign in"',
        step_id: "s1",
        at: "2026-10-02T10:00:00Z",
      }),
      ev({
        type: "gate.resolved",
        seq: 4,
        gate_id: "gc",
        action: "accept",
        actor: ADA,
        at: "2026-10-02T10:03:00Z",
      }),
    );
    expect(controls(v)).toContain("Ada handed back the browser");
  });

  it("a pause's resolve reads as a pause even with its tree row gone - never 'answered'", () => {
    const opened = fold(
      initialView(detail({ status: "running" })),
      stepRunning(2),
      ev({ type: "task.status", seq: 3, status: "paused", actor: ADA }),
      ev({
        type: "gate.opened",
        seq: 4,
        gate_id: "gp",
        kind: "pause",
        title: "Paused",
        step_id: "s1",
        at: "2026-10-02T10:00:00Z",
      }),
    );
    expect(opened.pauseGates).toEqual({ gp: true });
    // the tree no longer holds the row (a collapsed or rebuilt tree): the view still knows
    const pruned: TaskView = { ...opened, feed: opened.feed.filter((i) => i.kind !== "execution") };
    const resumed = fold(
      pruned,
      ev({
        type: "gate.resolved",
        seq: 5,
        gate_id: "gp",
        action: "accept",
        actor: BOB,
        at: "2026-10-02T10:02:00Z",
      }),
      ev({ type: "task.status", seq: 6, status: "running", actor: BOB }),
    );
    expect(controls(resumed)).toEqual(["Ada paused", "Bob resumed"]);
    expect(JSON.stringify(resumed.feed)).not.toMatch(/answered/);
    expect(resumed.gates).toEqual([]);
  });

  it.each([
    ["the record first, then the live end", true],
    ["the live end first, then the record", false],
  ])("a call a steer held stays held live - %s", (_, recordFirst) => {
    // The held call's live end reads ok (its result is the hold's words), the durable
    // `tool.called` says it did not run - the record wins whatever the order.
    const start = ev({
      type: "activity",
      kind: "tool",
      phase: "start",
      step_id: "s1",
      tool: "frame.write_file",
      tool_call_id: "tc1",
      label: "/work/todo.py",
    });
    const end = ev({
      type: "activity",
      kind: "tool",
      phase: "end",
      step_id: "s1",
      tool: "frame.write_file",
      tool_call_id: "tc1",
      label: "/work/todo.py",
      ok: true,
      duration_ms: 100,
    });
    const held = ev({
      type: "tool.called",
      seq: 3,
      step_id: "s1",
      tool: "write_file",
      ok: false,
      summary: "",
      duration_ms: 0,
      denied_reason: "held: Bob steered",
      tool_call_id: "tc1",
    });
    const v = fold(
      initialView(detail({ status: "running" })),
      stepRunning(2),
      start,
      ...(recordFirst ? [held, end] : [end, held]),
    );
    const rows = toolRows(v).filter((r) => r.id === "tc1");
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ ok: false, deniedReason: "held: Bob steered", pending: false });
  });

  it("a paused task whose step parks on a question stays paused - Resume stays offered", () => {
    // The backend keeps a PAUSED task paused when its step opens a question (no status frame):
    // the view must not flip it to waiting, or the menu's Resume (status === "paused") is gone
    // for good - replay would derive the same loss.
    const paused = fold(
      initialView(detail({ status: "running" })),
      stepRunning(2),
      ev({ type: "task.status", seq: 3, status: "paused", actor: ADA }),
      { ...GATE_OPENED, seq: 4, step_id: "s1" } as TaskEvent,
    );
    expect(paused.status).toBe("paused");
    expect(paused.pause).toEqual({ by: "Ada", applied: false });
    expect(paused.gates.map((g) => g.gate_id)).toEqual(["g1"]);
    // the resume door: running (Bob resumed), then waiting - the question is still open
    const resumed = fold(
      paused,
      ev({ type: "task.status", seq: 5, status: "running", actor: BOB }),
      ev({ type: "task.status", seq: 6, status: "waiting" }),
    );
    expect(resumed.status).toBe("waiting");
    expect(resumed.pause).toBeNull();
    expect(controls(resumed)).toEqual(["Ada paused", "Bob resumed"]);
    expect(resumed.gates.map((g) => g.gate_id)).toEqual(["g1"]);
  });

  it("a question on a task that is not paused still waits, as it always did", () => {
    const v = fold(initialView(detail({ status: "running" })), GATE_OPENED);
    expect(v.status).toBe("waiting");
  });

  it("an approval a steer superseded says so - never 'declined'", () => {
    const v = fold(
      initialView(detail({ status: "running" })),
      stepRunning(2),
      ev({
        type: "gate.opened",
        seq: 3,
        gate_id: "ga",
        kind: "approve",
        title: "Send the email",
        step_id: "s1",
      }),
      steer(4, "c1", 2),
      ev({
        type: "gate.resolved",
        seq: 5,
        gate_id: "ga",
        action: "decline",
        content: { superseded_by: "c1" },
        actor: BOB,
      }),
    );
    expect(controls(v)).toEqual(["Superseded by Bob's steer"]);
    expect(toolRows(v).find((r) => r.id === "gate-ga")?.summary).toBe("Superseded by Bob's steer");
    expect(v.gates).toEqual([]);
  });

  it("a steer sent while a question waits is named on the dock; one sent before it is not", () => {
    const opened = fold(initialView(detail({ status: "running" })), steer(2, "c0", 2), GATE_OPENED);
    expect(steerWhileWaiting(opened, opened.gates[0], "me")).toBeNull();
    const steered = fold(opened, steer(11, "c1", 3));
    expect(steerWhileWaiting(steered, steered.gates[0], "me")).toBe(
      "Bob steered while this waited; it applies after the answer",
    );
    expect(steerWhileWaiting(steered, steered.gates[0], "bob-id")).toBe(
      "You steered while this waited; it applies after the answer",
    );
    const applied = fold(
      steered,
      ev({
        type: "steer.applied",
        seq: 12,
        command_id: "c1",
        step_title: "Write",
        safe_point: "before_model",
      }),
    );
    expect(steerWhileWaiting(applied, applied.gates[0], "me")).toBeNull();
  });
});

describe("dockFor - the face an open gate's dock wears", () => {
  const opened = (over: Record<string, unknown>) =>
    reduce(initialView(detail()), {
      type: "gate.opened",
      seq: 5,
      task_id: "t1",
      gate_id: "g1",
      title: "Budget reached",
      questions: null,
      ...over,
    } as unknown as TaskEvent).gates[0];

  it("a budget park has no questions and no verbs: no dock, never a stepper over nothing", () => {
    // the live crash (1a UI check): a stepper mounted over [] read .type of undefined
    expect(dockFor(opened({ kind: "budget" }))).toBe("none");
  });

  it("a question batch gets the stepper; approve and connect get their own faces", () => {
    const batch = {
      title: "One question",
      questions: [
        {
          id: "q1",
          prompt: "Which?",
          type: "text",
          options: [],
          required: true,
        },
      ],
    };
    expect(dockFor(opened({ kind: "input", questions: batch }))).toBe("questions");
    expect(dockFor(opened({ kind: "approve" }))).toBe("approve");
    expect(dockFor(opened({ kind: "connect" }))).toBe("connect");
  });

  it("a pause gate draws nothing at all - nobody answers a pause, the resume lifts it", () => {
    expect(dockFor({ kind: "pause", questions: [] })).toBeNull();
  });

  it("a control gate wears its own face (ADR-065), never a stepper or Allow", () => {
    expect(dockFor(opened({ kind: "control", title: "Ada took control of the browser" }))).toBe(
      "control",
    );
  });

  it("a Skip on a control gate reads as a skip, never as skipped questions", () => {
    const v = fold(
      initialView(detail()),
      ev({
        type: "gate.opened",
        seq: 5,
        task_id: "t1",
        gate_id: "g1",
        kind: "control",
        title: "The agent needs someone to sign in to admin.example.com",
        questions: null,
      }),
      ev({
        type: "gate.resolved",
        seq: 6,
        task_id: "t1",
        gate_id: "g1",
        action: "decline",
        actor: { type: "user", id: "bob-id", name: "Bob" },
      }),
    );
    expect(v.feed.flatMap((i) => (i.kind === "control" ? [i.text] : []))).toContain("Bob skipped");
  });
});

describe("machine chip revalidation", () => {
  const v0 = () => initialView(detail());
  it("bumps machineVersion when a step's hold on the Machine ends or pauses", () => {
    const frames = [
      ev({ type: "task.delivered", seq: 1 }),
      ev({ type: "task.status", seq: 1, status: "failed" }),
      ev({ type: "task.status", seq: 1, status: "cancelled" }),
      ev({ type: "step.state", seq: 1, state: "done", title: "s", step_id: "s1" }),
      ev({ type: "step.state", seq: 1, state: "retrying", title: "s", step_id: "s1" }),
      ev({ type: "gate.opened", seq: 1, gate_id: "g1", kind: "ask", questions: [] }),
    ];
    for (const f of frames) expect(fold(v0(), f).machineVersion).toBeGreaterThan(0);
  });
  it("bumps machineVersion when a step takes the Machine (a warm bind has no machine.* event)", () => {
    const took = ev({
      type: "activity",
      kind: "engine",
      phase: "end",
      tool: "machine",
      step_id: "s1",
      label: "Preparing the Machine",
    });
    expect(fold(v0(), took).machineVersion).toBeGreaterThan(0);
  });
  it("leaves machineVersion alone for an unrelated event", () => {
    expect(fold(v0(), ev({ type: "task.status", seq: 1, status: "planning" })).machineVersion).toBe(
      0,
    );
    // the engine's own facts, and a Machine phase that has only begun, move no hold
    expect(
      fold(
        v0(),
        ev({
          type: "activity",
          kind: "engine",
          phase: "end",
          step_id: "s1",
          label: "Collected 1 deliverable",
        }),
      ).machineVersion,
    ).toBe(0);
    expect(
      fold(
        v0(),
        ev({
          type: "activity",
          kind: "engine",
          phase: "start",
          tool: "machine",
          step_id: "s1",
          label: "Waking the Machine",
        }),
      ).machineVersion,
    ).toBe(0);
  });
  it("tracks the Machine image and words an upgrade wake", () => {
    const v = fold(
      v0(),
      ev({ type: "machine.created", seq: 1, profile: "base" }),
      ev({ type: "machine.woke", seq: 2, turn: 1, profile: "browser" }),
    );
    expect(v.machineProfile).toBe("browser");
    const texts = v.feed.flatMap((i) => (i.kind === "marker" ? [i.text] : []));
    expect(texts).toContain("Machine woke on the browser image");
  });
  it("a wake in the same turn is not a follow-up; one in a later turn is", () => {
    const ada = { type: "user", id: "a1", name: "Ada" };
    const texts = (v: ReturnType<typeof v0>) =>
      v.feed.flatMap((i) => (i.kind === "marker" ? [i.text] : []));
    const same = fold(
      v0(),
      ev({ type: "machine.created", seq: 2, profile: "base" }),
      ev({ type: "machine.woke", seq: 3, turn: 1, profile: "base", actor: ada }),
    );
    expect(texts(same)).toContain("Machine woke");
    expect(texts(same).join()).not.toContain("follow-up");
    const later = fold(
      same,
      ev({ type: "machine.woke", seq: 4, turn: 2, profile: "base", actor: ada }),
    );
    expect(texts(later)).toContain("Machine woke for Ada's follow-up");
  });
  it("draws who drove the browser and how it ended, and re-reads the chip", () => {
    const bob = { type: "user", id: "b1", name: "Bob" };
    const v = fold(
      v0(),
      ev({ type: "machine.control_claimed", seq: 1, private: true, actor: bob }),
      ev({ type: "machine.control_ended", seq: 2, reason: "released", held_s: 100, actor: bob }),
    );
    const texts = v.feed.flatMap((i) => (i.kind === "marker" ? [i.text] : []));
    expect(texts).toEqual([
      "Bob took control of the browser (private)",
      "Bob handed back control after 1m 40s",
    ]);
    expect(v.machineVersion).toBe(2);
  });
  it("keeps the last ending: whose control, who ended it, why and when", () => {
    const bob = { type: "user", id: "b1", name: "Bob" };
    const owner = { type: "user", id: "p1", name: "Ada" };
    expect(v0().controlEnded).toBeNull();
    const claimed = fold(v0(), ev({ type: "machine.control_claimed", seq: 1, actor: bob }));
    expect(claimed.controlEnded).toBeNull();
    const ended = fold(
      claimed,
      ev({
        type: "machine.control_ended",
        seq: 2,
        reason: "ended",
        held_s: 30,
        actor: bob,
        ended_by: owner,
        at: "2026-10-02T12:00:30Z",
      }),
    );
    expect(ended.controlEnded).toEqual({
      holderId: "b1",
      endedBy: { id: "p1", name: "Ada" },
      reason: "ended",
      at: "2026-10-02T12:00:30Z",
    });
    const next = fold(
      ended,
      ev({ type: "machine.control_claimed", seq: 3, actor: bob }),
      ev({
        type: "machine.control_ended",
        seq: 4,
        reason: "released",
        held_s: 5,
        actor: bob,
        at: "2026-10-02T12:01:00Z",
      }),
    );
    expect(next.controlEnded).toEqual({
      holderId: "b1",
      endedBy: null,
      reason: "released",
      at: "2026-10-02T12:01:00Z",
    });
    // any other machine.* event leaves the last ending as it was
    expect(
      fold(next, ev({ type: "machine.slept", seq: 5, reason: "idle", after_s: 600 })).controlEnded,
    ).toEqual(next.controlEnded);
  });
});

describe("the screen's lease (ADR-065)", () => {
  it("keeps who drives from screen.state, draws nothing, and clears on a null holder", () => {
    const v = fold(
      initialView(detail()),
      ev({
        type: "screen.state",
        lane: "live",
        holder: { type: "user", id: "a1", name: "Ada" },
        private: true,
        until: "2026-10-02T12:10:00Z",
      }),
    );
    expect(v.screen).toEqual({
      holder: { id: "a1", name: "Ada" },
      private: true,
      until: "2026-10-02T12:10:00Z",
    });
    expect(v.feed).toEqual([]);
    expect(fold(v, ev({ type: "screen.state", lane: "live", holder: null })).screen).toBeNull();
  });

  it("starts from the lease the task's Machine carries", () => {
    const control = {
      holder: { id: "b1", name: "Bob" },
      private: false,
      since: "2026-10-02T12:00:00Z",
      until: "2026-10-02T12:01:00Z",
    };
    const v = initialView(detail({ machine: { state: "idle", profile: "browser", control } }));
    expect(v.screen).toEqual({
      holder: { id: "b1", name: "Bob" },
      private: false,
      until: "2026-10-02T12:01:00Z",
    });
    expect(initialView(detail()).screen).toBeNull();
  });
});

describe("a stale screen.state is repaired from the task read", () => {
  it("counts every screen.state, and reseedScreen restores the lease the task read carries", () => {
    const control = {
      holder: { id: "b1", name: "Bob" },
      private: false,
      since: "2026-10-02T12:00:00Z",
      until: "2026-10-02T12:01:00Z",
    };
    let v = initialView(detail());
    expect(v.screenVersion).toBe(0);
    v = fold(
      v,
      ev({
        type: "screen.state",
        lane: "live",
        holder: { type: "user", id: "a1", name: "Ada" },
        private: true,
        until: "2026-10-02T12:10:00Z",
      }),
    );
    expect(v.screenVersion).toBe(1);
    // the read says Bob holds it (the frame was stale): the page shows Bob
    const fixed = reseedScreen(
      v,
      detail({ machine: { state: "idle", profile: "browser", control } }),
    );
    expect(fixed.screen).toEqual({
      holder: { id: "b1", name: "Bob" },
      private: false,
      until: "2026-10-02T12:01:00Z",
    });
    expect(reseedScreen(v, detail()).screen).toBeNull();
  });
});

describe("screen.action - where the agent just acted (ADR-065)", () => {
  it("keeps the newest act's verb and box, and never touches the thread or the record", () => {
    const v0 = initialView(detail());
    const v = fold(
      v0,
      ev({
        type: "screen.action",
        task_id: "t1",
        at: "2026-10-02T09:00:00Z",
        verb: "click",
        box: { x: 10, y: 20, w: 100, h: 32 },
        host: "admin.example.com",
        path: "/users",
      }),
      ev({
        type: "screen.action",
        task_id: "t1",
        at: "2026-10-02T09:00:01Z",
        verb: "type",
        box: null,
        host: "admin.example.com",
        path: "/users",
      }),
    );
    expect(v.screenAction).toEqual({
      verb: "type",
      box: null,
      at: "2026-10-02T09:00:01Z",
    });
    expect(v.appliedSeq).toBe(v0.appliedSeq);
    expect(v.feed).toEqual(v0.feed);
  });
});

describe("a guarded step's ending (agent-loop §3.2)", () => {
  it("the step's group wears the newest frame's outcome, and a frame without one clears it", () => {
    const parked = fold(
      initialView(detail()),
      stepRunning(2),
      stepRunning(3, { outcome: "blocked", blocked_by: "login" }),
    );
    expect(exec(parked)?.groups.find((g) => g.id === "s1")).toMatchObject({
      outcome: "blocked",
      blockedBy: "login",
    });
    const resumed = fold(
      parked,
      stepRunning(4, { detail: "resumed: the browser was handed back" }),
    );
    expect(exec(resumed)?.groups.find((g) => g.id === "s1")?.outcome).toBeUndefined();
  });
});

describe("verification spend (verification-honesty) - the receipt's own cost", () => {
  const spend = (seq: number, stepId: string | null, cost: string) =>
    ev({ type: "usage.recorded", seq, step_id: stepId, model: "judge", cost_usd: cost });
  const signed = (seq: number, verdict: string) =>
    ev({
      type: "verification.signed",
      seq,
      step_id: "s1",
      artifact_id: "a1",
      verdict,
      suite: "document-suite",
      verifier_family: "anthropic",
      signature: "sig",
    });
  const epilogue = (v: TaskView) => {
    const item = v.feed.find((i) => i.kind === "epilogue");
    return item?.kind === "epilogue" ? item : undefined;
  };

  it("spend after Delivered on the producing step becomes the receipt's cost, not an unlabelled rise", () => {
    const v = fold(
      initialView(detail()),
      stepRunning(1),
      spend(2, "s1", "0.16"),
      ev({ type: "task.delivered", seq: 3 }),
      spend(4, "s1", "0.30"),
      spend(5, "s1", "0.07"),
      signed(6, "fail"),
    );
    expect(v.usageUsd).toBeCloseTo(0.53);
    expect(v.verifyUsd).toBeCloseTo(0.37);
    expect(v.verification?.costUsd).toBeCloseTo(0.37);
    expect(epilogue(v)).toMatchObject({ verdict: "fail", artifactId: "a1" });
    expect(epilogue(v)?.checkUsd).toBeCloseTo(0.37);
    expect(v.lateUsd).toEqual({});
  });

  it("spend before Delivered, or on another step, is never counted as verification", () => {
    const v = fold(
      initialView(detail()),
      stepRunning(1),
      spend(2, "s1", "0.16"),
      ev({ type: "task.delivered", seq: 3 }),
      spend(4, null, "0.02"),
      spend(5, "s2", "0.03"),
      signed(6, "pass"),
    );
    expect(v.verifyUsd).toBe(0);
    expect(epilogue(v)?.checkUsd).toBeUndefined();
  });
});
