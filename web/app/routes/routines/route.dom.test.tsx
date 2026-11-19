/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The routines list and its peek: rows read the When back as a sentence, opening one loads its
// detail and ledger, and the peek verbs (Run now, Pause, Resume, Retire, token) hit the right
// wire path and refresh. Quiet runs fold into a streak, and a run with a task opens the thread.
import { toast } from "@prismoid/blocks/toast";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import Routines, { whenSentence } from "./route";

type Props = ComponentProps<typeof Routines>;
type Row = Props["loaderData"]["items"][number];

const row = (id: string, over: Partial<Row> = {}): Row => ({
  budget_usd_per_run: 2,
  catch_up: "one",
  consecutive_failures: 0,
  created_at: "2026-11-01T10:00:00Z",
  events: null,
  id,
  instruction: `Do the ${id} thing`,
  keeper: "one",
  last_fired_at: null,
  max_runs: null,
  monthly_cap_usd: null,
  name: `Routine ${id}`,
  next_fire_at: null,
  notify: "done",
  overlap: "skip",
  owner_id: "u1",
  pause_note: null,
  paused_reason: null,
  posture: "go",
  project_id: null,
  runs_minted: 0,
  schedule: { cron: "0 9 * * 1-5" },
  spend_mtd_usd: 1,
  status: "active",
  trigger_kind: "schedule",
  until_at: null,
  upcoming: [],
  ...over,
});

const run = (id: string, decision: string, over: Record<string, unknown> = {}) => ({
  at: "2026-11-01T10:00:00Z",
  decision,
  detail: "",
  id,
  occurrence: id,
  scheduled_for: null,
  source: "schedule",
  task_id: null,
  task_status: null,
  ...over,
});

const ok = (data: unknown) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });

let detail: Row;
let runs: unknown[];
let get: ReturnType<typeof vi.spyOn>;
let post: ReturnType<typeof vi.spyOn>;
let del: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  detail = row("a");
  runs = [];
  get = vi.spyOn(api, "GET").mockImplementation(((path: string) => {
    if (path === "/v1/routines") return ok({ items: [row("a")], next_cursor: null });
    if (path === "/v1/routines/{routine_id}") return ok(detail);
    if (path === "/v1/routines/{routine_id}/runs") return ok({ items: runs, next_cursor: null });
    return Promise.resolve({
      data: undefined,
      error: { error: { code: "nf", message: "no" } },
      response: new Response(null, { status: 404 }),
    });
  }) as never);
  post = vi.spyOn(api, "POST").mockImplementation((() => ok({})) as never);
  del = vi.spyOn(api, "DELETE").mockImplementation((() => ok({})) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
  document.body.innerHTML = "";
});

function mount(items: Row[], url = "/routines") {
  const router = createMemoryRouter(
    [
      {
        path: "/routines",
        element: <Routines {...({ loaderData: { items, cursor: null } } as unknown as Props)} />,
      },
    ],
    { initialEntries: [url] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

const posted = (path: string) => post.mock.calls.filter((c: unknown[]) => c[0] === path);

describe("whenSentence", () => {
  it("reads each trigger as one sentence", () => {
    expect(
      whenSentence(row("x", { schedule: { cron: "1 2 * * *", timezone: "Asia/Tokyo" } })),
    ).toBe("cron 1 2 * * * · Asia/Tokyo");
    expect(whenSentence(row("x", { schedule: { every_s: 1800 } }))).toBe("every 30m");
    expect(whenSentence(row("x", { schedule: { every_s: 7200 } }))).toBe("every 2h");
    expect(
      whenSentence(
        row("x", { schedule: null, events: { kinds: ["task.failed", "task.delivered"] } }),
      ),
    ).toBe("when task.failed or task.delivered");
    expect(whenSentence(row("x", { schedule: null, trigger_kind: "manual" }))).toBe("manual");
  });
});

describe("Routines list", () => {
  it("an empty list says how to make one", () => {
    mount([]);
    expect(screen.getByText("No routines yet")).toBeDefined();
  });

  it("rows show name, When, keeper, failures and status", () => {
    mount([row("a", { keeper: "team", consecutive_failures: 3 }), row("b", { status: "paused" })]);
    expect(screen.getByText("Routine a")).toBeDefined();
    expect(screen.getByText(/kept by team/)).toBeDefined();
    expect(screen.getByText("3 failing")).toBeDefined();
    expect(screen.getByText("paused")).toBeDefined();
  });

  it("opening a row loads its detail and the ledger into the peek, URL-borne", async () => {
    const user = userEvent.setup();
    runs = [run("r1", "minted", { task_id: "t1", task_status: "done" })];
    detail = row("a", { upcoming: ["2026-11-02T09:00:00Z"], posture: "hold", max_runs: 5 });
    const router = mount([row("a")]);
    await user.click(screen.getByRole("button", { name: /Routine a/ }));
    expect(await screen.findByText("Do the a thing")).toBeDefined();
    expect(router.state.location.search).toBe("?routine=a");
    expect(screen.getByText(/runs wait for your release/)).toBeDefined();
    expect(screen.getByText("0/5 runs")).toBeDefined();
    expect(screen.getByText("overlap skip")).toBeDefined();
    expect(await screen.findByText(/schedule · .* · task done/)).toBeDefined();
  });

  it("an empty ledger says no fires yet and quiet runs fold into one streak line", async () => {
    mount([row("a")], "/routines?routine=a");
    expect(await screen.findByText(/No fires yet/)).toBeDefined();
    cleanup();
    runs = [run("q1", "skipped_budget"), run("q2", "skipped_cap"), run("e1", "error")];
    mount([row("a")], "/routines?routine=a");
    expect(await screen.findByText(/2 quiet fires \(skipped budget, skipped cap\)/)).toBeDefined();
  });

  it("a failed detail load toasts and a run with a task swaps in the thread with a way back", async () => {
    const user = userEvent.setup();
    runs = [run("r1", "minted", { task_id: "t1" })];
    const router = mount([row("a")], "/routines?routine=a");
    await user.click(await screen.findByRole("button", { name: /minted/ }));
    await waitFor(() => expect(router.state.location.search).toContain("run=t1"));
    await user.click(await screen.findByRole("button", { name: "Back to the routine" }));
    await waitFor(() => expect(router.state.location.search).toBe("?routine=a"));
    expect(await screen.findByText("Do the a thing")).toBeDefined();
  });

  it("an unreadable routine toasts instead of hanging", async () => {
    const error = vi.spyOn(toast, "error");
    get.mockImplementation(((path: string) =>
      path === "/v1/routines/{routine_id}"
        ? Promise.resolve({
            data: undefined,
            error: { error: { code: "nf", message: "Gone." } },
            response: new Response(null, { status: 404 }),
          })
        : ok({ items: [], next_cursor: null })) as never);
    mount([row("a")], "/routines?routine=a");
    await waitFor(() => expect(error).toHaveBeenCalledWith("Gone."));
  });
});

describe("Routine peek verbs", () => {
  it("Run now fires, toasts and reloads the ledger", async () => {
    const user = userEvent.setup();
    const success = vi.spyOn(toast, "success");
    mount([row("a")], "/routines?routine=a");
    await user.click(await screen.findByRole("button", { name: "Run now" }));
    await waitFor(() => expect(posted("/v1/routines/{routine_id}/fire")).toHaveLength(1));
    expect(success).toHaveBeenCalledWith("Fired - the run shows below.");
    await waitFor(() => {
      const ledgerReads = get.mock.calls.filter(
        (c: unknown[]) => c[0] === "/v1/routines/{routine_id}/runs",
      );
      expect(ledgerReads.length).toBeGreaterThan(1);
    });
  });

  it("Pause sends an empty body; a paused routine offers Resume and shows its note", async () => {
    const user = userEvent.setup();
    mount([row("a")], "/routines?routine=a");
    detail = row("a", { status: "paused", pause_note: "too noisy", paused_reason: "owner" });
    await user.click(await screen.findByRole("button", { name: "Pause" }));
    expect(posted("/v1/routines/{routine_id}/pause")[0]?.[1]).toEqual({
      params: { path: { routine_id: "a" } },
      body: {},
    });
    expect(await screen.findByText("Paused (owner): too noisy")).toBeDefined();
    await user.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(posted("/v1/routines/{routine_id}/resume")).toHaveLength(1));
  });

  it("a retired routine offers neither Run now nor Pause", async () => {
    detail = row("a", { status: "retired" });
    mount([row("a")], "/routines?routine=a");
    expect(await screen.findByText("Do the a thing")).toBeDefined();
    expect(screen.queryByRole("button", { name: "Run now" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Pause" })).toBeNull();
  });

  it("a failed verb toasts the server's message", async () => {
    const user = userEvent.setup();
    const error = vi.spyOn(toast, "error");
    post.mockImplementation((() =>
      Promise.resolve({
        data: undefined,
        error: { error: { code: "x", message: "Already firing." } },
        response: new Response(null, { status: 409 }),
      })) as never);
    mount([row("a")], "/routines?routine=a");
    await user.click(await screen.findByRole("button", { name: "Run now" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("Already firing."));
  });

  it("the menu mints a token, revokes it, and retires the routine then closes the peek", async () => {
    const user = userEvent.setup();
    post.mockImplementation(((path: string) =>
      ok(path.endsWith("/token") ? { token: "rt_secret" } : {})) as never);
    const success = vi.spyOn(toast, "success");
    const router = mount([row("a")], "/routines?routine=a");
    await user.click(await screen.findByRole("button", { name: "More" }));
    await user.click(await screen.findByRole("menuitem", { name: "Mint fire token…" }));
    await waitFor(() =>
      expect(success).toHaveBeenCalledWith(
        expect.stringContaining("Fire token copied"),
        expect.objectContaining({ description: "rt_secret" }),
      ),
    );
    await user.click(screen.getByRole("button", { name: "More" }));
    await user.click(await screen.findByRole("menuitem", { name: "Revoke fire token" }));
    await waitFor(() => expect(del).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "More" }));
    await user.click(await screen.findByRole("menuitem", { name: "Retire routine" }));
    await waitFor(() => expect(posted("/v1/routines/{routine_id}/retire")).toHaveLength(1));
    await waitFor(() => expect(router.state.location.search).toBe(""));
  });
});
