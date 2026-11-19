/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// A bot's Runs and Artifacts shelves, driven: runs list with cost, retries and a stats strip
// that only appears once the bot has run, artifacts list with filename and goal, both have an
// honest empty state, and a failed load says so rather than showing an empty shelf.
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import { ArtifactsTab, RunsTab } from "./shelves";

const ok = (data: unknown) => ({
  data,
  error: undefined,
  response: new Response(null, { status: 200 }),
});
const run = (id: string, over: Record<string, unknown> = {}) => ({
  task_id: id,
  title: `Run ${id}`,
  goal: `Goal for ${id}`,
  state: "done",
  attempt: 1,
  cost_usd: "0.25",
  at: "2026-11-01T10:00:00Z",
  ...over,
});
const stats = (over: Record<string, unknown> = {}) => ({
  runs: 2,
  spend_usd_30d: "1.5",
  spend_usd_total: "4",
  memory_count: 3,
  routine_count: 0,
  last_run_at: null,
  ...over,
});

let runs: unknown;
let artifacts: unknown;
let statsOut: unknown;
beforeEach(() => {
  runs = [];
  artifacts = [];
  statsOut = stats();
  vi.spyOn(api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/runs")) return Promise.resolve(ok(runs));
    if (path.endsWith("/stats")) return Promise.resolve(ok(statsOut));
    return Promise.resolve(ok(artifacts));
  }) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

const wrap = (el: React.ReactNode) => render(<MemoryRouter>{el}</MemoryRouter>);

describe("Runs tab", () => {
  it("shows an empty state when the bot has not run", async () => {
    wrap(<RunsTab name="scout" />);
    expect(await screen.findByText("No runs yet")).toBeTruthy();
  });

  it("lists runs with cost and shows the goal only when the title does not echo it", async () => {
    runs = [
      run("a", { title: "Find leads", goal: "Find leads for Q3" }),
      run("b", { goal: "Something else", attempt: 3 }),
    ];
    wrap(<RunsTab name="scout" />);
    expect(await screen.findByText("Find leads")).toBeTruthy();
    expect(screen.queryByText("Find leads for Q3")).toBeNull();
    expect(screen.getByText("Something else")).toBeTruthy();
    expect(screen.getByText("×3")).toBeTruthy();
    expect(screen.getAllByText("$0.25")).toHaveLength(2);
  });

  it("opens the stats strip with spend, runs and memories", async () => {
    runs = [run("a")];
    wrap(<RunsTab name="scout" />);
    expect(
      await screen.findByText(/\$1\.50 last 30 days · \$4\.00 all time · 2 runs as scout/),
    ).toBeTruthy();
    expect(screen.getByText(/3 memories/)).toBeTruthy();
    expect(screen.queryByText(/routines/)).toBeNull();
  });

  it("leaves the strip out when the stats say zero runs", async () => {
    runs = [run("a")];
    statsOut = stats({ runs: 0 });
    wrap(<RunsTab name="scout" />);
    await screen.findByText("Run a");
    expect(screen.queryByText(/all time/)).toBeNull();
  });

  it("keeps the loader up when the load fails", async () => {
    vi.spyOn(api, "GET").mockResolvedValue({
      data: undefined,
      error: { error: { code: "x", message: "down" } },
      response: new Response(null, { status: 500 }),
    } as never);
    wrap(<RunsTab name="scout" />);
    expect(screen.getByText("Loading runs…")).toBeTruthy();
    expect(screen.queryByText("No runs yet")).toBeNull();
  });
});

describe("Artifacts tab", () => {
  it("shows an empty state when nothing was published", async () => {
    wrap(<ArtifactsTab name="scout" />);
    expect(await screen.findByText("Nothing published yet")).toBeTruthy();
  });

  it("lists each artifact with its type and the goal it came from", async () => {
    artifacts = [
      {
        id: "x1",
        filename: "report.md",
        type: "document",
        goal: "Summarise Q3",
        verdict: null,
        at: "2026-11-01T10:00:00Z",
      },
    ];
    wrap(<ArtifactsTab name="scout" />);
    expect(await screen.findByRole("button", { name: /report\.md/ })).toBeTruthy();
    expect(screen.getByText("document · from “Summarise Q3”")).toBeTruthy();
  });
});
