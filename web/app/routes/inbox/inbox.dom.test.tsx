/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// Inbox triage, driven: a click or the arrow keys move the selection and mark the item read on
// the wire, Enter opens what the item points at, and Mark all read clears the unread dots. Items
// here carry no task, so the detail pane never reaches for one.
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TOPBAR_START_SLOT_ID } from "~/components/layout/topbar-slot";
import { api } from "~/lib/api/client";
import InboxRoute from "./route";

type Props = ComponentProps<typeof InboxRoute>;
type Item = Props["loaderData"]["items"][number];

const item = (id: string, over: Partial<Item> = {}): Item => ({
  id,
  kind: "project_shared",
  title: `Shared ${id}`,
  body: `Body of ${id}`,
  created_at: "2026-11-01T10:00:00Z",
  gate_id: null,
  payload: { project_id: `p-${id}` },
  project_id: null,
  read: false,
  task_id: null,
  task_title: null,
  ...over,
});

const items = [
  item("a"),
  item("b"),
  item("c", { read: true }),
  item("q", { kind: "question", title: "Report asks: which quarter?", payload: {} }),
];

let post: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  post = vi.spyOn(api, "POST").mockImplementation((() =>
    Promise.resolve({
      data: {},
      error: undefined,
      response: new Response(null, { status: 200 }),
    })) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

function mount() {
  // The topbar's start slot is where Mark all read portals to, as in the shell.
  const slot = document.createElement("div");
  slot.id = TOPBAR_START_SLOT_ID;
  document.body.append(slot);
  const router = createMemoryRouter(
    [
      {
        path: "/inbox",
        element: <InboxRoute {...({ loaderData: { items, unread: 3 } } as unknown as Props)} />,
      },
      { path: "/projects/:id", element: <p>project page</p> },
    ],
    { initialEntries: ["/inbox"] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

const rows = () => screen.getAllByRole("button").filter((b) => b.hasAttribute("aria-current"));
const current = () => rows().find((b) => b.getAttribute("aria-current") === "true");
const readCalls = () =>
  post.mock.calls.filter((c: unknown[]) => c[0] === "/v1/inbox/{item_id}/read");

describe("Inbox triage", () => {
  it("opens on the first item, selected", () => {
    mount();
    expect(current()?.textContent).toContain("Shared a");
  });

  it("ArrowDown moves to the next item and marks it read; ArrowUp moves back", async () => {
    const user = userEvent.setup();
    mount();
    await user.keyboard("{ArrowDown}");
    expect(current()?.textContent).toContain("Shared b");
    await waitFor(() => expect(readCalls()).toHaveLength(1));
    expect(readCalls()[0]?.[1]).toEqual({ params: { path: { item_id: "b" } } });
    await user.keyboard("{ArrowUp}");
    expect(current()?.textContent).toContain("Shared a");
  });

  it("an item already read is never marked again", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(rows()[2]!);
    expect(current()?.textContent).toContain("Shared c");
    expect(readCalls()).toHaveLength(0);
  });

  it("Enter opens what the selected item points at", async () => {
    const user = userEvent.setup();
    const router = mount();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(router.state.location.pathname).toBe("/projects/p-a"));
    expect(screen.getByText("project page")).toBeDefined();
  });

  it("a question knock wears its own kind in the detail pane", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(rows()[3]!);
    expect(current()?.textContent).toContain("Report asks: which quarter?");
    expect(screen.getByText(/^Questions · /)).toBeDefined();
  });

  it("Mark all read sends once and leaves nothing unread", async () => {
    const user = userEvent.setup();
    mount();
    const markAll = await screen.findByRole("button", { name: "Mark all read" });
    await user.click(markAll);
    const readAll = () => post.mock.calls.filter((c: unknown[]) => c[0] === "/v1/inbox/read-all");
    await waitFor(() => expect(readAll()).toHaveLength(1));
    await act(async () => {});
    // Nothing unread is left, so the button has nothing more to do.
    expect(markAll.hasAttribute("disabled")).toBe(true);
  });
});
