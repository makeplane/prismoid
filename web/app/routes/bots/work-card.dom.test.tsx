/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The work card in a bot's chat, driven: it starts as "starting" until the task is read, shows
// a live card's phase and plan from the task's stream, settles to done / failed / cancelled
// with the deliverable chip, and Reply, Open deliverable and Details act on the right task.
// Watching it is watching the task: news on the card marks the task seen, opening it does not.
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import { initialView } from "~/lib/task-view";

const stream = vi.hoisted(() => ({ view: null as unknown }));
vi.mock("~/hooks/use-task-stream", () => ({
  useTaskStream: (_id: string, detail: unknown) => ({
    view: stream.view ?? detail,
  }),
}));
const seen = vi.hoisted(() => ({ calls: [] as unknown[][] }));
vi.mock("~/hooks/use-seen-task", () => ({
  useSeenTask: (...args: unknown[]) => {
    seen.calls.push(args);
  },
}));

import { WorkCard } from "./work-card";

type Rows = Parameters<typeof WorkCard>[0]["rows"];
type Detail = Parameters<typeof initialView>[0];

const row = (seq: number, kind: string, over: Record<string, unknown> = {}) => ({
  seq,
  kind,
  text: "",
  detail: "",
  gate_id: null,
  artifact_ids: [],
  ...over,
});
const rowsOf = (...r: ReturnType<typeof row>[]) => r as unknown as Rows;

const detail = (over: Record<string, unknown> = {}) =>
  ({
    id: "t1",
    status: "running",
    goal: "Find leads",
    title: "Lead hunt",
    project_id: null,
    turn: 1,
    cost: { total_usd: "0.10" },
    plan: {
      steps: [
        { id: "s1", title: "Search", state: "done", agent: "researcher", attempt: 1, why: "" },
        { id: "s2", title: "Write up", state: "running", agent: "writer", attempt: 1, why: "" },
        { id: "s3", title: "Check", state: "pending", agent: null, attempt: 1, why: "" },
      ],
    },
    ...over,
  }) as unknown as Detail;

const ok = (data: unknown) => ({
  data,
  error: undefined,
  response: new Response(null, { status: 200 }),
});

let get: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  stream.view = null;
  seen.calls = [];
  get = vi.spyOn(api, "GET").mockImplementation((() => Promise.resolve(ok(detail()))) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

function mount(rows: Rows, over: Partial<Parameters<typeof WorkCard>[0]> = {}) {
  const props = {
    onOpenArtifact: vi.fn(),
    onReply: vi.fn(),
    onGateResolved: vi.fn(),
  };
  const card = (now: Rows) => (
    <MemoryRouter>
      <WorkCard
        taskId="t1"
        projectId={null}
        rows={now}
        botSeq={undefined}
        name="scout"
        gates={{}}
        {...props}
        {...over}
      />
    </MemoryRouter>
  );
  const view = render(card(rows));
  return { ...props, rerender: (now: Rows) => view.rerender(card(now)) };
}

const started = row(1, "started", { text: "Started a task: Find leads" });

describe("A live card", () => {
  it("says starting under the row's title until the task is read", () => {
    get.mockImplementation((() => new Promise(() => {})) as never);
    mount(rowsOf(started));
    expect(screen.getByText("Find leads")).toBeTruthy();
    expect(screen.getByText(/starting/)).toBeTruthy();
  });

  it("shows the task's title, plan and the stream's phase and elapsed time", async () => {
    stream.view = {
      ...initialView(detail()),
      live: { phase: "writing", elapsed_s: 75 },
    };
    mount(rowsOf(started));
    expect(await screen.findByText("Lead hunt")).toBeTruthy();
    expect(screen.getByText("Search")).toBeTruthy();
    expect(screen.getByText("researcher")).toBeTruthy();
    expect(screen.getByText("Check")).toBeTruthy();
    expect(screen.getByText(/writing/)).toBeTruthy();
    expect(screen.getByText(/1m/)).toBeTruthy();
  });

  it("settles when the task's own stream reports a terminal status", async () => {
    stream.view = { ...initialView(detail()), status: "cancelled" };
    mount(rowsOf(started));
    expect(await screen.findByText("cancelled")).toBeTruthy();
  });

  it("says unknown when the task cannot be read", async () => {
    get.mockResolvedValue({
      data: undefined,
      error: { error: { code: "x", message: "gone" } },
      response: new Response(null, { status: 404 }),
    } as never);
    mount(rowsOf(started));
    expect(await screen.findByText("Couldn't load the task.")).toBeTruthy();
    expect(screen.getByText("unknown")).toBeTruthy();
  });

  it("shows a failure that came after the turn started", async () => {
    stream.view = { ...initialView(detail()), live: { phase: "working", elapsed_s: 0 } };
    mount(rowsOf(started, row(2, "failed", { text: "Step failed", detail: "Out of budget" })));
    expect(await screen.findByText("Out of budget")).toBeTruthy();
  });
});

describe("A settled card", () => {
  const delivered = row(3, "delivered", { artifact_ids: ["a1", "a2"] });

  it("shows done, the finished plan and one chip per deliverable", async () => {
    get.mockResolvedValue(ok(detail({ status: "delivered" })) as never);
    const { onOpenArtifact } = mount(rowsOf(started, delivered));
    expect(await screen.findByText("done")).toBeTruthy();
    expect(await screen.findByText("Write up")).toBeTruthy();
    const chips = screen.getAllByRole("button", { name: "Open deliverable" });
    expect(chips).toHaveLength(2);
    await userEvent.setup().click(chips[1]);
    expect(onOpenArtifact).toHaveBeenCalledWith("a2");
  });

  it("explains a failed turn in place", async () => {
    mount(rowsOf(started, row(3, "failed", { text: "Failed", detail: "Tool crashed" })));
    expect(await screen.findByText("failed")).toBeTruthy();
    expect(screen.getByText("Tool crashed")).toBeTruthy();
  });

  it("explains a cancelled turn in place", async () => {
    mount(rowsOf(started, row(3, "cancelled", { text: "Stopped by you" })));
    expect(await screen.findByText("cancelled")).toBeTruthy();
    expect(screen.getByText("Stopped by you")).toBeTruthy();
  });

  it("is settled by the bot's later line even with no settle row", async () => {
    mount(rowsOf(started), { botSeq: 5 });
    expect(await screen.findByText("done")).toBeTruthy();
  });

  it("still says how it ended when the task cannot be read", async () => {
    get.mockResolvedValue({
      data: undefined,
      error: { error: { code: "x", message: "gone" } },
      response: new Response(null, { status: 500 }),
    } as never);
    mount(rowsOf(started, delivered));
    expect(await screen.findByText("Couldn't load the task.")).toBeTruthy();
    expect(screen.getByText("done")).toBeTruthy();
  });
});

describe("Card footer", () => {
  it("Reply hands back the task and its title", async () => {
    const { onReply } = mount(rowsOf(started, row(3, "delivered")));
    await userEvent.setup().click(await screen.findByRole("button", { name: "Reply" }));
    expect(onReply).toHaveBeenCalledWith("t1", "Lead hunt");
  });

  it("Details links to the task page under its project", async () => {
    get.mockResolvedValue(ok(detail({ project_id: "p9" })) as never);
    mount(rowsOf(started, row(3, "delivered")));
    const link = await screen.findByRole("link", { name: /Details/ });
    await screen.findByText("Search");
    expect(link.getAttribute("href")).toContain("t1");
    expect(link.getAttribute("href")).toContain("p9");
  });
});

describe("Watching the card", () => {
  it("marks the task seen when news comes, never on open", async () => {
    stream.view = initialView(detail());
    const { rerender } = mount(rowsOf(started));
    await screen.findByText("Lead hunt");
    const opened = seen.calls[seen.calls.length - 1];
    expect(opened).toEqual(["t1", "1", { onOpen: false }]);
    rerender(rowsOf(started, row(3, "delivered")));
    expect(seen.calls[seen.calls.length - 1]).toEqual(["t1", "3", { onOpen: false }]);
  });
});
