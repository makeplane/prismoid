/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The task browser, driven: status groups with their true counts, the debounced search sending
// its query, bulk select (checkbox, Select all, Ctrl+A, Escape) then archive or confirmed
// delete on the wire, an empty list, and the other groupings and the board layout.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskWindowProvider } from "~/components/tasks/task-window";
import { api } from "~/lib/api/client";
import { TaskBrowser } from "./browser";
import type { TaskSummary } from "./data";

const task = (id: string, status: string, over: Record<string, unknown> = {}) =>
  ({
    id,
    goal: `Goal ${id}`,
    title: null,
    status,
    mode: "standard",
    project_id: null,
    created_at: "2026-10-01T10:00:00Z",
    archived_at: null,
    pinned_at: null,
    ...over,
  }) as unknown as TaskSummary;

const fresh = (): Record<string, TaskSummary[]> => ({
  running: [task("r1", "running"), task("r2", "planning", { mode: "fleet" })],
  delivered: [task("d1", "delivered")],
  failed: [],
});
let DATA = fresh();
const reply = (data: unknown) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });

let get: ReturnType<typeof vi.spyOn>;
let post: ReturnType<typeof vi.spyOn>;
let del: ReturnType<typeof vi.spyOn>;
type Query = { status?: string[]; q?: string };
const queries = (): Query[] =>
  get.mock.calls.map((c: unknown[]) => (c[1] as { params: { query: Query } }).params.query);

beforeEach(() => {
  DATA = fresh();
  localStorage.clear();
  get = vi.spyOn(api, "GET").mockImplementation(((path: string, init: unknown) => {
    if (path === "/v1/tasks/counts") return reply({ counts: { running: 2, delivered: 1 } });
    const status = (init as { params: { query: Query } }).params.query.status ?? [];
    const items = status.includes("running")
      ? DATA.running
      : status.includes("delivered")
        ? DATA.delivered
        : status.length
          ? DATA.failed
          : [...DATA.running, ...DATA.delivered];
    return reply({ items, next_cursor: null });
  }) as never);
  post = vi.spyOn(api, "POST").mockImplementation((() => reply({})) as never);
  del = vi.spyOn(api, "DELETE").mockImplementation((() => reply({})) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
  document.body.innerHTML = "";
});

function mount(url = "/", props: Partial<React.ComponentProps<typeof TaskBrowser>> = {}) {
  const router = createMemoryRouter(
    [
      {
        path: "*",
        element: (
          <TaskWindowProvider initial={{ pins: [], recents: [] }}>
            <TaskBrowser {...props} />
          </TaskWindowProvider>
        ),
      },
    ],
    { initialEntries: [url] },
  );
  return render(<RouterProvider router={router} />);
}

describe("TaskBrowser lists", () => {
  it("groups tasks by status with their server totals", async () => {
    mount();
    expect(await screen.findByText("Goal r1")).toBeDefined();
    expect(screen.getByText("Goal d1")).toBeDefined();
    const inProgress = screen.getByRole("heading", { name: "In progress" }).closest("section")!;
    await waitFor(() =>
      expect(within(inProgress).getByText("2", { selector: "span" })).toBeDefined(),
    );
    expect(screen.queryByRole("heading", { name: "Failed & cancelled" })).toBeNull();
    expect(screen.getByText("fleet")).toBeDefined();
  });

  it("the search box sends the settled query to the server", async () => {
    const user = userEvent.setup();
    mount();
    await screen.findByText("Goal r1");
    await user.type(screen.getByRole("textbox", { name: "Search tasks…" }), "pricing");
    await waitFor(() => expect(queries().some((q) => q.q === "pricing")).toBe(true));
    expect(queries().some((q) => q.q === "pric")).toBe(false);
  });

  it("an empty answer says there is nothing yet, and a viewer is not invited to start", async () => {
    DATA.running = [];
    DATA.delivered = [];
    mount("/", { canStart: false });
    expect(await screen.findByText("Nothing here yet")).toBeDefined();
    expect(screen.getByText("Tasks started in this Project show up here.")).toBeDefined();
  });

  it("buckets one stream by mode when grouped by mode", async () => {
    mount("/?group=mode", { syncToUrl: true });
    expect(await screen.findByRole("heading", { name: "fleet" })).toBeDefined();
    expect(screen.getByRole("heading", { name: "standard" })).toBeDefined();
    expect(queries().some((q) => q.status === undefined)).toBe(true);
  });

  it("the board layout draws one column per status group", async () => {
    mount("/?view=board", { syncToUrl: true });
    expect(await screen.findByRole("region", { name: "In progress" })).toBeDefined();
    expect(screen.getByRole("region", { name: "Delivered" })).toBeDefined();
    expect(screen.getByRole("region", { name: "Failed & cancelled" })).toBeDefined();
  });

  it("an archived filter in the URL asks the server for archived tasks only", async () => {
    mount("/?archived=1", { syncToUrl: true });
    await screen.findByText("Goal r1");
    expect(
      get.mock.calls.some((c: unknown[]) => JSON.stringify(c[1]).includes('"archived":true')),
    ).toBe(true);
    expect(screen.getByRole("button", { name: "Remove filter Archived" })).toBeDefined();
  });
});

describe("TaskBrowser bulk actions", () => {
  async function select(name: string) {
    await screen.findByText(`Goal ${name}`);
    fireEvent.click(screen.getByRole("checkbox", { name: `Select Goal ${name}` }));
  }

  it("selecting rows shows the bar, and archive posts one call per task", async () => {
    const user = userEvent.setup();
    mount();
    await select("r1");
    await select("d1");
    expect(screen.getByText("2 selected")).toBeDefined();
    await user.click(screen.getByRole("button", { name: "Archive" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    const paths = post.mock.calls.map((c: unknown[]) => [
      c[0],
      (c[1] as never as { params: unknown }).params,
    ]);
    expect(paths).toContainEqual(["/v1/tasks/{task_id}/archive", { path: { task_id: "r1" } }]);
    expect(paths).toContainEqual(["/v1/tasks/{task_id}/archive", { path: { task_id: "d1" } }]);
    await waitFor(() => expect(screen.queryByText("2 selected")).toBeNull());
  });

  it("Select all picks every loaded row and the X clears the selection", async () => {
    const user = userEvent.setup();
    mount();
    await select("r1");
    await user.click(screen.getByRole("button", { name: /Select all/ }));
    expect(screen.getByText("3 selected")).toBeDefined();
    await user.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(screen.queryByText("3 selected")).toBeNull();
  });

  it("Ctrl+A selects the loaded rows and Escape drops the selection", async () => {
    mount();
    await screen.findByText("Goal r1");
    fireEvent.keyDown(window, { key: "a", ctrlKey: true });
    expect(await screen.findByText("3 selected")).toBeDefined();
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByText("3 selected")).toBeNull());
  });

  it("delete asks first, Cancel sends nothing, Delete removes each task", async () => {
    const user = userEvent.setup();
    mount();
    await select("r1");
    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(await screen.findByText("Delete 1 task?")).toBeDefined();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(del).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Delete" }));
    await user.click(await screen.findByRole("button", { name: "Delete" }));
    await waitFor(() => expect(del).toHaveBeenCalledTimes(1));
    expect(del.mock.calls[0][1]).toEqual({ params: { path: { task_id: "r1" } } });
  });

  it("an archive the server refuses is reported and the rest still go", async () => {
    const user = userEvent.setup();
    post.mockImplementationOnce((() =>
      Promise.resolve({
        data: undefined,
        error: { detail: "task is live" },
        response: new Response(null, { status: 409 }),
      })) as never);
    mount();
    await select("r1");
    await select("d1");
    await user.click(screen.getByRole("button", { name: "Archive" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText("2 selected")).toBeNull());
  });
});
