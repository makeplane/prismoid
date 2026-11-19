/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The sidebar's task list: pinned above latest, the creator's own rows offer rename, pin and
// a confirmed delete (a teammate's row offers nothing), display options persist, and a full
// window ends with a View all tail.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SidebarProvider } from "@prismoid/blocks/sidebar";
import { TaskWindowProvider, type TaskWindowRow } from "~/components/tasks/task-window";
import { api } from "~/lib/api/client";
import { dayLabel } from "~/lib/format";
import { STORAGE } from "~/lib/storage";
import { SidebarRecentTasks } from "./sidebar-recent-tasks";

const row = (id: string, at: string, over: Partial<TaskWindowRow> = {}): TaskWindowRow => ({
  id,
  label: `Task ${id}`,
  status: "delivered",
  pinned: false,
  createdAt: at,
  creatorId: "me",
  ...over,
});
const reply = (data: unknown) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });

beforeEach(() => {
  localStorage.clear();
  vi.stubGlobal(
    "matchMedia",
    (q: string) =>
      ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }) as never,
  );
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
  document.body.innerHTML = "";
});

function mount(
  seed: { pins: TaskWindowRow[]; recents: TaskWindowRow[] },
  at = "/",
  me: string | null = "me",
) {
  const page = (
    <SidebarProvider>
      <TaskWindowProvider initial={seed}>
        <SidebarRecentTasks />
      </TaskWindowProvider>
    </SidebarProvider>
  );
  const router = createMemoryRouter(
    [
      {
        id: "routes/shell/route",
        path: "/",
        loader: () => ({ user: me ? { id: me } : undefined }),
        children: [
          { index: true, element: page },
          { path: "*", element: page },
        ],
      },
    ],
    { initialEntries: [at] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

const SEED = {
  pins: [row("p1", "2026-10-03T00:00:00Z", { pinned: true })],
  recents: [
    row("r1", "2026-10-02T00:00:00Z"),
    row("r2", "2026-10-01T00:00:00Z", { creatorId: "x" }),
  ],
};

describe("SidebarRecentTasks", () => {
  it("lists pinned and latest tasks as links to their pages", async () => {
    mount(SEED);
    expect(await screen.findByText("Pinned")).toBeDefined();
    expect(screen.getByText("Recent tasks")).toBeDefined();
    const link = screen.getByRole("link", { name: /Task r1/ });
    expect(link.getAttribute("href")).toBe("/tasks/r1");
  });

  it("renders nothing when there are no tasks", async () => {
    const { container } = render(<div />);
    mount({ pins: [], recents: [] });
    await waitFor(() => expect(screen.queryByText("Recent tasks")).toBeNull());
    expect(container.textContent).toBe("");
  });

  it("only the creator's rows have an actions menu", async () => {
    mount(SEED);
    await screen.findByText("Pinned");
    expect(screen.getByRole("button", { name: "Task actions for Task r1" })).toBeDefined();
    expect(screen.queryByRole("button", { name: "Task actions for Task r2" })).toBeNull();
  });

  it("Rename edits in place and PATCHes the title on Enter", async () => {
    const user = userEvent.setup();
    const patch = vi.spyOn(api, "PATCH").mockImplementation((() => reply({})) as never);
    mount(SEED);
    await user.click(await screen.findByRole("button", { name: "Task actions for Task r1" }));
    await user.click(await screen.findByRole("menuitem", { name: "Rename" }));
    const field = screen.getByRole("textbox", { name: "Task name" });
    await user.clear(field);
    await user.type(field, "Better name{Enter}");
    await waitFor(() => expect(patch).toHaveBeenCalled());
    expect(patch.mock.calls[0][1]).toMatchObject({
      params: { path: { task_id: "r1" } },
      body: { title: "Better name" },
    });
    expect(await screen.findByText("Better name")).toBeDefined();
  });

  it("Escape abandons a rename without a request", async () => {
    const user = userEvent.setup();
    const patch = vi.spyOn(api, "PATCH");
    mount(SEED);
    await user.click(await screen.findByRole("button", { name: "Task actions for Task r1" }));
    await user.click(await screen.findByRole("menuitem", { name: "Rename" }));
    await user.type(screen.getByRole("textbox", { name: "Task name" }), "zzz{Escape}");
    expect(screen.queryByRole("textbox", { name: "Task name" })).toBeNull();
    expect(patch).not.toHaveBeenCalled();
  });

  it("Pin moves the task up to Pinned and PATCHes pinned", async () => {
    const user = userEvent.setup();
    const patch = vi.spyOn(api, "PATCH").mockImplementation((() => reply({})) as never);
    mount(SEED);
    await user.click(await screen.findByRole("button", { name: "Task actions for Task r1" }));
    await user.click(await screen.findByRole("menuitem", { name: "Pin" }));
    await waitFor(() => expect(patch).toHaveBeenCalled());
    expect(patch.mock.calls[0][1]).toMatchObject({ body: { pinned: true } });
  });

  it("Delete asks first, and confirming removes the row and leaves its open page", async () => {
    const user = userEvent.setup();
    const del = vi.spyOn(api, "DELETE").mockImplementation((() => reply({})) as never);
    vi.spyOn(api, "GET").mockImplementation((() => reply({ items: [] })) as never);
    const router = mount(SEED, "/tasks/r1");
    await user.click(await screen.findByRole("button", { name: "Task actions for Task r1" }));
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));
    expect(await screen.findByText("Delete this task?")).toBeDefined();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(del).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Task actions for Task r1" }));
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));
    await user.click(await screen.findByRole("button", { name: "Delete task" }));
    await waitFor(() => expect(del).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(router.state.location.pathname).toBe("/tasks"));
    expect(screen.queryByText("Task r1")).toBeNull();
  });

  it("Group by date adds day headers and remembers the choice", async () => {
    const user = userEvent.setup();
    mount(SEED);
    await user.click(await screen.findByRole("button", { name: "Tasks section options" }));
    await user.click(await screen.findByRole("menuitemcheckbox", { name: "Group by date" }));
    await waitFor(() => expect(localStorage.getItem(STORAGE.sidebarTasksGroup)).toBe("date"));
    expect(screen.getByText(dayLabel(new Date("2026-10-02T00:00:00Z")))).toBeDefined();
  });

  it("hiding status marks is remembered", async () => {
    const user = userEvent.setup();
    mount(SEED);
    await user.click(await screen.findByRole("button", { name: "Tasks section options" }));
    await user.click(await screen.findByRole("menuitemcheckbox", { name: "Show status" }));
    await waitFor(() => expect(localStorage.getItem(STORAGE.sidebarTasksMarks)).toBe("off"));
  });

  it("a full window ends with a link to all tasks", async () => {
    const many = Array.from({ length: 20 }, (_, i) =>
      row(`m${i}`, `2026-09-${String(i + 1).padStart(2, "0")}T00:00:00Z`),
    );
    mount({ pins: [], recents: many });
    const all = await screen.findByRole("link", { name: "View all tasks" });
    expect(all.getAttribute("href")).toBe("/tasks");
  });
});
