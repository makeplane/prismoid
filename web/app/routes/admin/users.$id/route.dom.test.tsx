/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// One account in the operator console: the header and status, then each verb behind its own
// confirm (suspend takes a reason, grant/revoke admin and sign-out-everywhere confirm) hitting
// the right wire path and refreshing the loader, plus the activity chips and the tab strip.
import { toast } from "@prismoid/blocks/toast";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import AdminUserDetail from "./route";

const ok = (data: unknown) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });

const user = (over: Record<string, unknown> = {}) => ({
  created_at: "2026-10-01T00:00:00Z",
  deleted: false,
  email: "ann@example.com",
  id: "u1",
  is_admin: false,
  last_seen_at: "2026-11-01T00:00:00Z",
  live_api_keys: 1,
  live_sessions: 2,
  name: "Ann Lee",
  spend_mtd_usd: "4.5",
  spend_total_usd: "20",
  status: "active",
  task_counts: { running: 2, failed: 1 },
  ...over,
});

let post: ReturnType<typeof vi.spyOn>;
let loads: number;
beforeEach(() => {
  post = vi.spyOn(api, "POST").mockImplementation((() => ok({})) as never);
  vi.spyOn(api, "GET").mockImplementation((() => ok({ items: [], next_cursor: null })) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

function mount(u = user(), url = "/users/u1") {
  loads = 0;
  const router = createMemoryRouter(
    [
      {
        path: "/users/:id",
        loader: () => {
          loads += 1;
          return { user: u, spend: [] };
        },
        Component: AdminUserDetail,
        HydrateFallback: () => null,
      },
    ],
    { initialEntries: [url] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

const confirm = async (u: ReturnType<typeof userEvent.setup>, trigger: string) => {
  await u.click(await screen.findByRole("button", { name: trigger }));
  const buttons = await screen.findAllByRole("button", { name: trigger });
  await u.click(buttons[buttons.length - 1]!);
};

describe("AdminUserDetail overview", () => {
  it("heads the page with the name, email, status, admin badge and spend", async () => {
    mount(user({ is_admin: true }));
    expect(await screen.findByRole("heading", { name: "Ann Lee" })).toBeDefined();
    expect(screen.getByText(/ann@example.com/)).toBeDefined();
    expect(screen.getByText("active")).toBeDefined();
    expect(screen.getByText("admin")).toBeDefined();
    expect(screen.getByText("2 live sessions · 1 API key")).toBeDefined();
    expect(screen.getByText(/this month/)).toBeDefined();
  });

  it("an activity chip flips to the Tasks tab with that status filtered, in the URL", async () => {
    const u = userEvent.setup();
    const router = mount();
    await u.click(await screen.findByRole("button", { name: /failed/ }));
    await waitFor(() => expect(router.state.location.search).toContain("tab=tasks"));
    expect(router.state.location.search).toContain("status=failed");
    expect(await screen.findByPlaceholderText("Search title, goal, or task ID…")).toBeDefined();
  });

  it("no tasks says so", async () => {
    mount(user({ task_counts: {} }));
    expect(await screen.findByText("No tasks yet")).toBeDefined();
  });

  it("the tab strip writes ?tab= and the default tab is the bare URL", async () => {
    const u = userEvent.setup();
    const router = mount();
    await u.click(await screen.findByRole("tab", { name: "History" }));
    await waitFor(() => expect(router.state.location.search).toBe("?tab=history"));
    await u.click(screen.getByRole("tab", { name: "Overview" }));
    await waitFor(() => expect(router.state.location.search).toBe(""));
  });
});

describe("AdminUserDetail verbs", () => {
  it("Suspend needs a reason, posts it trimmed, toasts and reloads", async () => {
    const u = userEvent.setup();
    const success = vi.spyOn(toast, "success");
    mount();
    await u.click(await screen.findByRole("button", { name: "Suspend" }));
    const dlg = await screen.findByRole("dialog");
    const go = Array.from(dlg.querySelectorAll("button")).find((b) => b.textContent === "Suspend")!;
    expect(go.hasAttribute("disabled")).toBe(true);
    await u.type(await screen.findByRole("textbox", { name: "Reason" }), "  spamming  ");
    await u.click(go);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post.mock.calls[0]?.[0]).toBe("/v1/admin/users/{id}/suspend");
    expect(post.mock.calls[0]?.[1]).toEqual({
      params: { path: { id: "u1" } },
      body: { reason: "spamming" },
    });
    expect(success).toHaveBeenCalledWith("Suspended.");
    await waitFor(() => expect(loads).toBe(2));
  });

  it("a suspended user offers Unsuspend, not Suspend", async () => {
    const u = userEvent.setup();
    mount(user({ status: "suspended" }));
    expect(await screen.findByText("suspended")).toBeDefined();
    expect(screen.queryByRole("button", { name: "Suspend" })).toBeNull();
    await confirm(u, "Unsuspend");
    await waitFor(() => expect(post.mock.calls[0]?.[0]).toBe("/v1/admin/users/{id}/unsuspend"));
  });

  it("Grant admin and Revoke admin each hit their own path", async () => {
    const u = userEvent.setup();
    mount();
    await confirm(u, "Grant admin");
    await waitFor(() => expect(post.mock.calls[0]?.[0]).toBe("/v1/admin/users/{id}/grant-admin"));
    cleanup();
    post.mockClear();
    mount(user({ is_admin: true }));
    await confirm(u, "Revoke admin");
    await waitFor(() => expect(post.mock.calls[0]?.[0]).toBe("/v1/admin/users/{id}/revoke-admin"));
  });

  it("Sign out everywhere reports how many credentials it revoked", async () => {
    const u = userEvent.setup();
    const success = vi.spyOn(toast, "success");
    post.mockImplementation((() => ok({ sessions_revoked: 2, api_keys_revoked: 1 })) as never);
    mount();
    await confirm(u, "Sign out everywhere");
    await waitFor(() => expect(success).toHaveBeenCalledWith("Revoked 2 sessions and 1 API key."));
  });

  it("a failed verb toasts and keeps the confirm open", async () => {
    const u = userEvent.setup();
    const error = vi.spyOn(toast, "error");
    post.mockImplementation((() =>
      Promise.resolve({
        data: undefined,
        error: { error: { code: "x", message: "Not allowed." } },
        response: new Response(null, { status: 403 }),
      })) as never);
    mount();
    await confirm(u, "Grant admin");
    await waitFor(() => expect(error).toHaveBeenCalledWith("Not allowed."));
    expect(screen.getByRole("alertdialog")).toBeDefined();
  });
});
