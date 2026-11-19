/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The Project lobby's Live now (ADR-062): the tasks that are moving right now, each with who
// asked for it and, when someone else is running the current turn, whose turn it is.
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("~/lib/api/client", async () => ({
  api: { GET: state.get },
  // The wire's failure is DATA here ({ err }) and unwrap raises it - a mock that returns a
  // rejected promise trips vitest's own rejection tracking even when the code handles it.
  unwrap: async (p: Promise<{ err?: Error }>) => {
    const v = await p;
    if (v?.err) throw v.err;
    return v;
  },
}));

import { fetchLiveTasks, LIVE_STATUSES, LiveNowRows } from "./live-now";

const task = (over: Record<string, unknown> = {}) => ({
  id: "t1",
  goal: "Research three competitors to Linear",
  title: null,
  status: "running",
  creator_id: "u-ada",
  turn_author_id: "u-ada",
  creator_name: "Ada",
  turn_author_name: "Ada",
  ...over,
});

const html = (tasks: ReturnType<typeof task>[], more = 0) =>
  renderToStaticMarkup(
    <MemoryRouter>
      <LiveNowRows tasks={tasks as never} more={more} />
    </MemoryRouter>,
  );

beforeEach(() => state.get.mockReset());

describe("the rows", () => {
  it("name the task, link to it, and say who asked for it", () => {
    const out = html([task()]);
    expect(out).toContain("Research three competitors to Linear");
    expect(out).toContain('href="/tasks/t1"');
    expect(out).toContain("by Ada");
    expect(out).toContain("Status: running");
  });

  it("link to the task under its Project (the canonical URL, no redirect hop)", () => {
    expect(html([task({ project_id: "s1" })])).toContain('href="/projects/s1/tasks/t1"');
  });

  it("prefer the creator's label to the goal", () => {
    expect(html([task({ title: "Linear rivals" })])).toContain("Linear rivals");
  });

  it("say whose turn it is only when it is not the creator's", () => {
    expect(html([task()])).not.toContain("turn:");
    const out = html([task({ turn_author_id: "u-bob", turn_author_name: "Bob" })]);
    expect(out).toContain("by Ada");
    expect(out).toContain("turn: Bob");
  });

  it("tell two people who share a name apart by id, never by the name", () => {
    // two Alexes: comparing names would hide that it is the OTHER Alex's turn
    const out = html([
      task({
        creator_id: "u-1",
        turn_author_id: "u-2",
        creator_name: "Alex",
        turn_author_name: "Alex",
      }),
    ]);
    expect(out).toContain("by Alex");
    expect(out).toContain("turn: Alex");
    // and one person under a changed display name is still one person
    expect(
      html([task({ creator_id: "u-1", turn_author_id: "u-1", turn_author_name: "Alexandra" })]),
    ).not.toContain("turn:");
  });

  it("stay quiet about a name the server could not give, rather than guess", () => {
    const out = html([task({ creator_name: null, turn_author_name: null })]);
    expect(out).not.toContain("by ");
    expect(out).not.toContain("Someone");
    // a turn author with no creator name is still worth saying
    expect(
      html([task({ creator_name: null, turn_author_id: "u-bob", turn_author_name: "Bob" })]),
    ).toContain("turn: Bob");
  });

  it("a name is text, never markup", () => {
    const out = html([task({ creator_name: "<b>Ada</b>" })]);
    expect(out).not.toContain("<b>");
    expect(out).toContain("&lt;b&gt;");
  });

  it("nothing running renders nothing at all, not an empty heading", () => {
    expect(html([])).toBe("");
  });

  it("past the page it says how many more are live, so the Project is not undercounted", () => {
    expect(html([task()], 3)).toContain("and 3 more");
    expect(html([task()], 0)).not.toContain("more");
  });
});

describe("what it asks for", () => {
  it("is this Project's planning, running and waiting tasks, one bounded page", async () => {
    state.get.mockResolvedValue({ items: [task()], next_cursor: null });
    const live = await fetchLiveTasks("sp1");
    expect(LIVE_STATUSES).toEqual(["planning", "running", "waiting"]);
    expect(state.get).toHaveBeenCalledTimes(1);
    expect(state.get).toHaveBeenCalledWith("/v1/tasks", {
      params: {
        query: { project_id: "sp1", status: ["planning", "running", "waiting"], limit: 20 },
      },
    });
    expect(live.tasks).toHaveLength(1);
    expect(live.more).toBe(0);
  });

  it("counts what the page did not hold, from the server's own tallies", async () => {
    const full = Array.from({ length: 20 }, (_, i) => task({ id: `t${i}` }));
    state.get.mockImplementation(async (path: string) =>
      path === "/v1/tasks"
        ? { items: full, next_cursor: "c1" }
        : { counts: { planning: 4, running: 15, waiting: 6, delivered: 90, paused: 2 } },
    );
    const live = await fetchLiveTasks("sp1");
    expect(state.get).toHaveBeenCalledWith("/v1/tasks/counts", {
      params: { query: { project_id: "sp1" } },
    });
    expect(live.tasks).toHaveLength(20);
    expect(live.more).toBe(5); // 4 + 15 + 6 live, 20 shown
  });

  it("a full page with nothing past it says no more: exactly the limit is not the limit plus one", async () => {
    const full = Array.from({ length: 20 }, (_, i) => task({ id: `t${i}` }));
    state.get.mockImplementation(async (path: string) =>
      path === "/v1/tasks"
        ? { items: full, next_cursor: "c1" } // a keyset page that is full carries a cursor
        : { counts: { planning: 2, running: 12, waiting: 6, delivered: 90 } },
    );
    const live = await fetchLiveTasks("sp1");
    expect(live.more).toBe(0);
    expect(html(live.tasks as never, live.more)).not.toContain("more");
    // and a tally that raced a task settling (fewer live than shown) never goes negative
    state.get.mockImplementation(async (path: string) =>
      path === "/v1/tasks" ? { items: full, next_cursor: "c1" } : { counts: { running: 18 } },
    );
    expect((await fetchLiveTasks("sp1")).more).toBe(0);
  });

  it("a failed tally still says there is more, never nothing", async () => {
    state.get.mockImplementation(async (path: string) => {
      if (path === "/v1/tasks") return { items: [task()], next_cursor: "c1" };
      return { err: new Error("counts down") };
    });
    const live = await fetchLiveTasks("sp1");
    expect(live.more).toBe(1);
  });
});
