/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The app sidebar: every nav destination links to its page and the current one is marked, the
// inbox badge caps at 99+, the account menu shows who is signed in and logs out by posting to
// /logout, and the edge rail resizes with the arrow keys and remembers the width.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SidebarProvider } from "@prismoid/blocks/sidebar";
import { TaskWindowProvider } from "~/components/tasks/task-window";
import { api } from "~/lib/api/client";
import type { User } from "~/lib/auth";
import { STORAGE } from "~/lib/storage";
import { AppSidebar } from "./app-sidebar";

const user = {
  id: "u1",
  name: "Ada Lovelace",
  email: "ada@example.com",
  workspace: { id: "w1", name: "Analytical", slug: "analytical" },
} as unknown as User;

beforeEach(() => {
  localStorage.clear();
  // the settings dialog loads its panels; nothing here talks to a server
  vi.spyOn(api, "GET").mockImplementation((() =>
    Promise.resolve({
      data: { items: [] },
      error: undefined,
      response: new Response(null, { status: 200 }),
    })) as never);
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

function mount(at = "/", unread = 0, onLogout = vi.fn()) {
  const page = (
    <SidebarProvider>
      <TaskWindowProvider initial={{ pins: [], recents: [] }}>
        <AppSidebar user={user} unread={unread} projects={[]} bots={[]} />
      </TaskWindowProvider>
    </SidebarProvider>
  );
  const router = createMemoryRouter(
    [
      { path: "/logout", action: onLogout, element: null },
      { path: "/", element: page },
      { path: "*", element: page },
    ],
    { initialEntries: [at] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

const NAV = [
  "Home",
  "Tasks",
  "Projects",
  "Routines",
  "Bots",
  "Connectors",
  "Allowed sites",
  "Inbox",
];

describe("AppSidebar navigation", () => {
  it("links every destination, and marks only the current one", async () => {
    mount("/routines");
    for (const label of NAV) {
      expect(await screen.findByRole("link", { name: label })).toBeDefined();
    }
    expect(screen.getByRole("link", { name: "Tasks" }).getAttribute("href")).toBe("/tasks");
    const current = NAV.filter((l) =>
      screen.getByRole("link", { name: l }).hasAttribute("data-active"),
    );
    expect(current).toEqual(["Routines"]);
  });

  it("a nested page keeps its section active", async () => {
    mount("/tasks/abc");
    await screen.findByRole("link", { name: "Tasks" });
    expect(screen.getByRole("link", { name: "Tasks" }).hasAttribute("data-active")).toBe(true);
    expect(screen.getByRole("link", { name: "Home" }).hasAttribute("data-active")).toBe(false);
  });

  it("the inbox badge shows the count, caps at 99+, and hides at zero", async () => {
    mount("/", 7);
    expect(await screen.findByText("7")).toBeDefined();
    cleanup();
    mount("/", 150);
    expect(await screen.findByText("99+")).toBeDefined();
    cleanup();
    mount("/", 0);
    await screen.findByRole("link", { name: "Inbox" });
    expect(screen.queryByText("0")).toBeNull();
  });
});

describe("AppSidebar new task", () => {
  it("the New task pen rides the Tasks row (and the rail) and links to Home's composer", async () => {
    mount("/routines");
    // One pen on the Tasks row, one under the logo on the rail (CSS shows one at a time).
    const pens = await screen.findAllByLabelText("New task");
    expect(pens).toHaveLength(2);
    for (const pen of pens) {
      expect(pen.tagName).toBe("A");
      expect(pen.getAttribute("href")).toBe("/");
    }
    const tasksRow = screen.getByRole("link", { name: "Tasks" }).closest("li");
    expect(tasksRow?.querySelector('[aria-label="New task"]')).not.toBeNull();
    // A verb, not a row: it never takes the "you are here" mark from Routines.
    expect(screen.getByRole("link", { name: "Routines" }).hasAttribute("data-active")).toBe(true);
  });

  it("Cmd+Shift+O and Ctrl+Shift+O open a new task from anywhere; Cmd+O alone does not", async () => {
    const userEv = userEvent.setup();
    const router = mount("/routines");
    await screen.findAllByLabelText("New task");
    await userEv.keyboard("{Meta>}o{/Meta}");
    expect(router.state.location.pathname).toBe("/routines");
    await userEv.keyboard("{Meta>}{Shift>}O{/Shift}{/Meta}");
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
    await router.navigate("/inbox");
    await userEv.keyboard("{Control>}{Shift>}O{/Shift}{/Control}");
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
  });
});

describe("AppSidebar account menu", () => {
  it("shows who is signed in and logs out with a POST to /logout", async () => {
    const userEv = userEvent.setup();
    const onLogout = vi.fn(() => null);
    mount("/", 0, onLogout);
    expect(await screen.findByText("ada@example.com")).toBeDefined();
    await userEv.click(screen.getByText("Ada Lovelace"));
    await userEv.click(await screen.findByRole("menuitem", { name: "Log out" }));
    await waitFor(() => expect(onLogout).toHaveBeenCalledTimes(1));
    const call = onLogout.mock.calls[0] as unknown as [{ request: Request }];
    expect(call[0].request.method).toBe("POST");
  });

  it("Settings opens the settings dialog", async () => {
    const userEv = userEvent.setup();
    mount();
    await userEv.click(await screen.findByText("Ada Lovelace"));
    await userEv.click(await screen.findByRole("menuitem", { name: "Settings" }));
    expect(await screen.findByRole("dialog")).toBeDefined();
  });
});

describe("AppSidebar edge", () => {
  it("arrow keys nudge the width and remember it", async () => {
    const userEv = userEvent.setup();
    mount();
    const rail = await screen.findByRole("button", { name: "Resize or toggle sidebar" });
    rail.focus();
    await userEv.keyboard("{ArrowRight}");
    expect(Number(localStorage.getItem(STORAGE.navWidth))).toBeGreaterThanOrEqual(208);
    expect(
      document
        .querySelector<HTMLElement>('[data-slot="sidebar-wrapper"]')
        ?.style.getPropertyValue("--sidebar-width"),
    ).toMatch(/px$/);
  });

  it("a click on the rail collapses the sidebar to icons", async () => {
    const userEv = userEvent.setup();
    mount();
    await userEv.click(await screen.findByRole("button", { name: "Resize or toggle sidebar" }));
    expect(await screen.findByRole("button", { name: "Expand sidebar" })).toBeDefined();
  });
});
