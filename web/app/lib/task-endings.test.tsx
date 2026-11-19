/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// A stopped turn says what got done, and an honest could-not reads as its
// own ending - never "Completed", never the red Failed mark, never a green Delivered.
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { StatusMark, statusLabel, taskMark } from "~/components/kit/status-badge";
import type { TaskEvent } from "~/lib/api/events";
import { rootLabel } from "~/routes/tasks.$id/progress-copy";
import { EpilogueBlock } from "~/routes/tasks.$id/epilogue";
import type { ExecGroup, FeedItem } from "./task-view";
import { initialView, reduce, type TaskView } from "./task-view";

const ev = (frame: Record<string, unknown>): TaskEvent => frame as unknown as TaskEvent;
const detail = () =>
  ({
    status: "created",
    goal: "two steps",
    cost: { total_usd: "0" },
    plan: { steps: [] },
  }) as never;
const fold = (v: TaskView, ...f: TaskEvent[]) => f.reduce(reduce, v);
const epilogue = (v: TaskView) =>
  v.feed.filter((i): i is Extract<FeedItem, { kind: "epilogue" }> => i.kind === "epilogue").at(-1);

const group = (over: Partial<ExecGroup>): ExecGroup => ({
  id: "s1",
  kind: "step",
  title: "Gather prices",
  state: "done",
  attempt: 1,
  rows: [],
  agent: "researcher",
  ...over,
});

describe("a cancelled turn says how far it got", () => {
  it("a tree stopped mid-step never reads 'Completed'", () => {
    // Live: a researcher step cut by the stop read "Completed 1 step" under a cancelled task.
    const label = rootLabel(
      { settled: true, groups: [group({}), group({ id: "s2", state: "running" })] },
      "cancelled",
    );
    expect(label).not.toMatch(/^Completed/);
    expect(label).toBe("Stopped - 1 of 2 steps done");
    // The step the stop cut off is marked skipped by the server a moment later: still not done.
    const cut = rootLabel(
      { settled: true, groups: [group({}), group({ id: "s2", state: "skipped" })] },
      "cancelled",
    );
    expect(cut).toBe("Stopped - 1 of 2 steps done");
    // A tree whose steps all finished did complete; the stop's account says what never ran.
    expect(rootLabel({ settled: true, groups: [group({})] }, "cancelled")).toMatch(/^Completed/);
  });

  it("the cancel's account lands as the turn's end, naming what was done and what never ran", () => {
    const account =
      "Stopped after 1 of 2 steps.\n- Done: Gather prices (prices.csv)\n- Not started: Write the table";
    const v = fold(
      initialView(detail()),
      ev({ type: "task.status", seq: 1, status: "running" }),
      ev({
        type: "task.status",
        seq: 2,
        status: "cancelled",
        reason: "stopped at the task's budget",
        summary: account,
        actor: { id: "u1", name: "Ada Lovelace", type: "user" },
      }),
    );
    const end = epilogue(v);
    expect(end?.stopped?.account).toBe(account);
    const html = renderToStaticMarkup(<EpilogueBlock item={end!} />);
    expect(html).toContain("Stopped by");
    expect(html).toContain("stopped at the task&#x27;s budget");
    expect(html).toContain("Done: Gather prices (prices.csv)");
    expect(html).toContain("Not started: Write the table");
    expect(html).not.toContain("Delivered");
  });
});

describe("the steps a stop never reached", () => {
  it("open no tree under the turn's end when the server marks them skipped", () => {
    const step = (seq: number, id: string, state: string) =>
      ev({
        type: "step.state",
        seq,
        step_id: id,
        title: id,
        agent: "researcher",
        state,
        attempt: 1,
      });
    const v = fold(
      initialView(detail()),
      ev({
        type: "task.planned",
        seq: 1,
        plan: {
          steps: [
            { id: "s1", title: "s1", state: "pending", agent: "researcher" },
            { id: "s2", title: "s2", state: "pending", agent: "writer" },
          ],
        },
      }),
      step(2, "s1", "running"),
      ev({
        type: "task.status",
        seq: 3,
        status: "cancelled",
        summary: "Stopped after 0 of 2 steps.",
      }),
      step(4, "s1", "skipped"),
      step(5, "s2", "skipped"),
    );
    const trees = v.feed.filter((i) => i.kind === "execution");
    const groups = trees.flatMap((i) => (i.kind === "execution" ? i.groups : []));
    expect(groups.some((g) => g.id === "s2")).toBe(false);
    expect(groups.find((g) => g.id === "s1")?.state).toBe("skipped"); // the cut step keeps its row
    expect(v.steps.find((s) => s.id === "s2")?.state).toBe("skipped");
    expect(v.feed.at(-1)?.kind).toBe("epilogue");
  });
});

describe("an honest could-not is not a crash", () => {
  it("the fold keeps task.status's ending", () => {
    const v = fold(
      initialView(detail()),
      ev({ type: "task.status", seq: 1, status: "running" }),
      ev({
        type: "task.status",
        seq: 2,
        status: "failed",
        reason: "operator could not do this: x",
        ending: "could_not",
      }),
    );
    expect(v.ending).toBe("could_not");
  });

  it("the status mark for failed+could_not is neutral and says so", () => {
    const html = renderToStaticMarkup(<StatusMark status={taskMark("failed", "could_not")} />);
    expect(html).not.toContain("text-destructive");
    expect(statusLabel(taskMark("failed", "could_not"))).toBe("couldn't do it");
    // A fault still reads as one.
    expect(renderToStaticMarkup(<StatusMark status={taskMark("failed", null)} />)).toContain(
      "text-destructive",
    );
  });
});

describe("a declined answer never reads Delivered", () => {
  it("delivered+could_not marks the turn 'Couldn't do it'", () => {
    const v = fold(
      initialView(detail()),
      ev({ type: "task.status", seq: 1, status: "running" }),
      ev({
        type: "task.delivered",
        seq: 2,
        summary: "I can't help with that.",
        ending: "could_not",
      }),
    );
    expect(v.ending).toBe("could_not");
    const html = renderToStaticMarkup(<EpilogueBlock item={epilogue(v)!} />);
    expect(html).toContain("Couldn&#x27;t do it");
    expect(html).not.toContain("Delivered");
    expect(statusLabel(taskMark("delivered", "could_not"))).toBe("couldn't do it");
  });
});

describe("a partly-done answer is not a green check", () => {
  it("delivered+partial marks the turn 'partly done'", () => {
    expect(taskMark("delivered", "partial")).toBe("partial");
    expect(statusLabel(taskMark("delivered", "partial"))).toBe("partly done");
  });
});
