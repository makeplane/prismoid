/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The operator task console and its peek: rows carry owner, spend and stuck reasons, status and
// stuck chips and the scope chips write the URL and refetch with matching query params, a row
// opens the peek (URL-borne) whose Cancel takes a reason, and Load more appends the next page.
import { toast } from "@prismoid/blocks/toast";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import { TOPBAR_SLOT_ID } from "~/components/layout/topbar-slot";
import { TaskConsole, type AdminTaskRow } from "./task-console";

const ok = (data: unknown) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });

const task = (id: string, over: Partial<AdminTaskRow> = {}): AdminTaskRow => ({
  created_at: "2026-11-01T10:00:00Z",
  goal_line: `Goal ${id}`,
  held: false,
  id,
  last_event_at: null,
  owner: { email: `${id}@example.com`, id: `u-${id}`, name: id },
  project_id: null,
  spend_usd: "0.25",
  status: "running",
  stuck_reason: null,
  title: `Task ${id}`,
  turn: 2,
  updated_at: "2026-11-01T10:00:00Z",
  ...over,
});

type Query = Record<string, unknown>;
let pages: Record<string, { items: AdminTaskRow[]; next_cursor: string | null }>;
let get: ReturnType<typeof vi.spyOn>;
let post: ReturnType<typeof vi.spyOn>;
const listCalls = (): Query[] =>
  get.mock.calls
    .filter((c: unknown[]) => c[0] === "/v1/admin/tasks")
    .map((c: unknown[]) => (c[1] as { params: { query: Query } }).params.query);

beforeEach(() => {
  pages = {
    first: { items: [task("a"), task("b", { stuck_reason: "no worker" })], next_cursor: null },
  };
  get = vi.spyOn(api, "GET").mockImplementation(((
    path: string,
    init: { params: { query?: Query; path?: Record<string, string> } },
  ) => {
    if (path === "/v1/admin/tasks")
      return ok(pages[String(init.params.query?.cursor ?? "first")] ?? pages.first);
    if (path === "/v1/admin/tasks/{task_id}") return ok(task(String(init.params.path?.task_id)));
    if (path === "/v1/admin/tasks/{task_id}/events") return ok({ events: [], next_since: null });
    if (path === "/v1/admin/users/{id}") return ok({ email: "scoped@example.com" });
    if (path === "/v1/admin/projects/{project_id}") return ok({ name: "Roadmap" });
    return ok({});
  }) as never);
  post = vi
    .spyOn(api, "POST")
    .mockImplementation((() => ok(task("a", { status: "cancelled" }))) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

function mount(props: Parameters<typeof TaskConsole>[0] = {}, url = "/tasks") {
  const slot = document.createElement("div");
  slot.id = TOPBAR_SLOT_ID;
  document.body.append(slot);
  const router = createMemoryRouter(
    [
      { path: "/tasks", element: <TaskConsole {...props} /> },
      { path: "/users/:id", element: <p>user page</p> },
    ],
    { initialEntries: [url] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

describe("TaskConsole list", () => {
  it("rows show title, owner link, turn, stuck reason and spend", async () => {
    mount();
    expect(await screen.findByText("Task a")).toBeDefined();
    expect(screen.getByRole("link", { name: "a@example.com" })).toBeDefined();
    expect(screen.getAllByText(/turn 2/).length).toBe(2);
    expect(screen.getByText("stuck - no worker")).toBeDefined();
  });

  it("a scoped console renders the owner as plain text and passes the scope to the wire", async () => {
    mount({ userId: "u-a" });
    expect(await screen.findByText("Task a")).toBeDefined();
    expect(screen.queryByRole("link", { name: "a@example.com" })).toBeNull();
    expect(listCalls()[0]).toMatchObject({ user_id: "u-a" });
  });

  it("an empty list says so and a search with no hits names the query", async () => {
    pages = { first: { items: [], next_cursor: null } };
    mount();
    expect(await screen.findByText("Nothing here")).toBeDefined();
    cleanup();
    mount({}, "/tasks?q=zzz");
    expect(await screen.findByText("No matches for “zzz”")).toBeDefined();
  });

  it("status chips toggle in the URL and refetch with the status list", async () => {
    const user = userEvent.setup();
    const router = mount();
    await screen.findByText("Task a");
    await user.click(screen.getByRole("button", { name: "running" }));
    await user.click(screen.getByRole("button", { name: "failed" }));
    expect(router.state.location.search).toBe("?status=running%2Cfailed");
    await waitFor(() =>
      expect(listCalls().some((q) => JSON.stringify(q.status) === '["running","failed"]')).toBe(
        true,
      ),
    );
    await user.click(screen.getByRole("button", { name: "running" }));
    expect(router.state.location.search).toBe("?status=failed");
  });

  it("Stuck sets the filter and a linked user chip names the user and clears on click", async () => {
    const user = userEvent.setup();
    const router = mount({}, "/tasks?user_id=u9");
    const chip = await screen.findByRole("button", { name: /scoped@example.com/ });
    await user.click(screen.getByRole("button", { name: "Stuck" }));
    expect(router.state.location.search).toContain("stuck=true");
    await waitFor(() => expect(listCalls().some((q) => q.stuck === true)).toBe(true));
    await user.click(chip);
    expect(router.state.location.search).not.toContain("user_id");
  });

  it("a linked project chip wears the project's name", async () => {
    mount({}, "/tasks?project_id=p1");
    expect(await screen.findByRole("button", { name: /Roadmap/ })).toBeDefined();
  });

  it("Load more asks for the next cursor and appends", async () => {
    const user = userEvent.setup();
    pages = {
      first: { items: [task("a")], next_cursor: "c2" },
      c2: { items: [task("z", { created_at: "2026-10-01T10:00:00Z" })], next_cursor: null },
    };
    mount();
    await user.click(await screen.findByRole("button", { name: /load more/i }));
    expect(await screen.findByText("Task z")).toBeDefined();
    expect(screen.getByText("Task a")).toBeDefined();
  });
});

describe("TaskConsole peek", () => {
  it("a row click opens the peek in the URL and Close puts it away", async () => {
    const user = userEvent.setup();
    const router = mount();
    await user.click(await screen.findByRole("button", { name: "Task a" }));
    expect(router.state.location.search).toBe("?task=a");
    expect(await screen.findByText("No durable frames yet.")).toBeDefined();
    await user.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(router.state.location.search).toBe(""));
  });

  it("Cancel needs a reason, posts it and toasts the new status", async () => {
    const user = userEvent.setup();
    const success = vi.spyOn(toast, "success");
    mount({}, "/tasks?task=a");
    await user.click(await screen.findByRole("button", { name: "Cancel task" }));
    await user.type(await screen.findByRole("textbox", { name: "Reason" }), "runaway spend");
    const confirms = screen.getAllByRole("button", { name: "Cancel task" });
    await user.click(confirms[confirms.length - 1]!);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post.mock.calls[0]?.[0]).toBe("/v1/admin/tasks/{task_id}/cancel");
    expect(post.mock.calls[0]?.[1]).toEqual({
      params: { path: { task_id: "a" } },
      body: { reason: "runaway spend" },
    });
    expect(success).toHaveBeenCalledWith("Cancelled. Status is now cancelled.");
  });

  it("a finished task offers no Cancel", async () => {
    pages = { first: { items: [task("a", { status: "delivered" })], next_cursor: null } };
    get.mockImplementation(((path: string) =>
      path === "/v1/admin/tasks"
        ? ok(pages.first)
        : path === "/v1/admin/tasks/{task_id}"
          ? ok(task("a", { status: "delivered", owner: null }))
          : ok({ events: [], next_since: null })) as never);
    mount({}, "/tasks?task=a");
    expect(await screen.findByText("No durable frames yet.")).toBeDefined();
    expect(screen.queryByRole("button", { name: "Cancel task" })).toBeNull();
  });
});
