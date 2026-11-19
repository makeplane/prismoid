/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The projects list, driven: rows link to their project, grouping by role rides the URL and
// buckets the loaded rows, Load more appends the next page, search refetches by query, and
// creating a project posts the trimmed name then opens it.
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TOPBAR_SLOT_ID } from "~/components/layout/topbar-slot";
import { api } from "~/lib/api/client";
import Projects from "./route";

type Props = ComponentProps<typeof Projects>;

const row = (id: string, role: string, over: Record<string, unknown> = {}) => ({
  id,
  name: `Project ${id}`,
  role,
  icon: null,
  icon_hue: null,
  budget_usd: null,
  created_at: "2026-11-01T10:00:00Z",
  ...over,
});

const ok = (data: unknown) => ({
  data,
  error: undefined,
  response: new Response(null, { status: 200 }),
});

let get: ReturnType<typeof vi.spyOn>;
let post: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  get = vi
    .spyOn(api, "GET")
    .mockImplementation((() => Promise.resolve(ok({ items: [], next_cursor: null }))) as never);
  post = vi
    .spyOn(api, "POST")
    .mockImplementation((() => Promise.resolve(ok({ id: "new-1" }))) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

function mount(items: unknown[], cursor: string | null = null) {
  const slot = document.createElement("div");
  slot.id = TOPBAR_SLOT_ID;
  document.body.append(slot);
  const router = createMemoryRouter(
    [
      {
        path: "/projects",
        element: <Projects {...({ loaderData: { items, cursor } } as unknown as Props)} />,
      },
      { path: "/projects/:id", element: <p>project page</p> },
    ],
    { initialEntries: ["/projects"] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

describe("Projects list", () => {
  it("lists each project as a link with its role", () => {
    mount([row("a", "owner"), row("b", "viewer")]);
    const link = screen.getByRole("link", { name: /Project a/ });
    expect(link.getAttribute("href")).toBe("/projects/a");
    expect(within(link).getByText("owner")).toBeTruthy();
    expect(screen.getByRole("link", { name: /Project b/ })).toBeTruthy();
  });

  it("shows the empty state when there are no projects", () => {
    mount([]);
    expect(screen.getByText("No projects yet")).toBeTruthy();
  });

  it("groups the loaded rows by role from the display menu and records it in the URL", async () => {
    const user = userEvent.setup();
    const router = mount([row("a", "viewer"), row("b", "owner"), row("c", "editor")]);
    await user.click(await screen.findByRole("button", { name: "Display options" }));
    await user.click(await screen.findByRole("menuitemradio", { name: "Role" }));
    expect(router.state.location.search).toBe("?group=role");
    const headings = screen.getAllByRole("heading").map((h) => h.textContent);
    expect(headings).toEqual(["owners", "editors", "viewers"]);
  });

  it("Load more fetches the next page with the cursor and appends it", async () => {
    const user = userEvent.setup();
    get.mockResolvedValueOnce(ok({ items: [row("z", "editor")], next_cursor: null }) as never);
    mount([row("a", "owner")], "cur-1");
    await user.click(screen.getByRole("button", { name: /load more/i }));
    expect(await screen.findByRole("link", { name: /Project z/ })).toBeTruthy();
    const query = (get.mock.calls[0][1] as { params: { query: Record<string, unknown> } }).params
      .query;
    expect(query.cursor).toBe("cur-1");
    expect(screen.getByRole("link", { name: /Project a/ })).toBeTruthy();
  });

  it("searching refetches by the typed query and names a miss", async () => {
    const user = userEvent.setup();
    mount([row("a", "owner")]);
    await user.type(await screen.findByPlaceholderText("Search projects…"), "zebra");
    expect(await screen.findByText("No matches for “zebra”")).toBeTruthy();
    const last = get.mock.calls.at(-1)?.[1] as { params: { query: Record<string, unknown> } };
    expect(last.params.query.q).toBe("zebra");
  });
});

describe("Creating a project", () => {
  async function openDialog() {
    const user = userEvent.setup();
    const router = mount([]);
    await user.click(await screen.findByRole("button", { name: "New project" }));
    return { user, router, input: await screen.findByRole("textbox", { name: "Name" }) };
  }

  it("keeps Create disabled until a name is typed, then posts it trimmed and opens it", async () => {
    const { user, router, input } = await openDialog();
    const create = screen.getByRole("button", { name: "Create project" });
    expect((create as HTMLButtonElement).disabled).toBe(true);
    await user.type(input, "  Research  ");
    await user.click(create);
    expect(post).toHaveBeenCalledWith("/v1/projects", { body: { name: "Research" } });
    await waitFor(() => expect(router.state.location.pathname).toBe("/projects/new-1"));
  });

  it("Enter in the name field creates too", async () => {
    const { user, router, input } = await openDialog();
    await user.type(input, "Ops{Enter}");
    await waitFor(() => expect(router.state.location.pathname).toBe("/projects/new-1"));
  });

  it("stays on the page when the create fails", async () => {
    post.mockResolvedValueOnce({
      data: undefined,
      error: { detail: "nope" },
      response: new Response(null, { status: 409 }),
    } as never);
    const { user, router, input } = await openDialog();
    await user.type(input, "Ops{Enter}");
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(router.state.location.pathname).toBe("/projects");
  });
});
