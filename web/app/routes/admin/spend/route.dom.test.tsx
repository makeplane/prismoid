/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The operator spend page: the summary tiles, model and spender rows (a spender with an account
// links to it), a zero-cost warning only when it applies, and the grouping chips that write the
// URL, fetch that grouping once and draw one legend pill per key.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import AdminSpend from "./route";

const ok = (data: unknown) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });

const summary = (over: Record<string, unknown> = {}) => ({
  by_model: [{ provider: "anthropic", model: "opus", calls: 12, cost_usd: "3.5" }],
  calls: 40,
  days: 30,
  mtd_usd: "10",
  price_versions: [{ version: 7, calls: 40 }],
  today_usd: "1",
  top_spenders: [
    { email: "ann@example.com", name: "Ann", user_id: "u1", calls: 20, cost_usd: "5" },
    { email: "ghost@example.com", name: "", user_id: null, calls: 2, cost_usd: "0.5" },
  ],
  total_usd: "25",
  zero_cost_calls: 0,
  ...over,
});

const day = (d: string, key: string | null, cost: string) => ({
  day: d,
  key,
  calls: 1,
  cost_usd: cost,
});
const totals = [day("2026-11-01", null, "1"), day("2026-11-02", null, "3")];

let get: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  get = vi
    .spyOn(api, "GET")
    .mockImplementation(((_p: string, init: { params: { query: { group?: string } } }) =>
      ok(
        init?.params?.query?.group === "model"
          ? [day("2026-11-01", "opus", "2"), day("2026-11-02", "mini", "1")]
          : totals,
      )) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

function mount(data = { summary: summary(), daily: totals }, url = "/spend") {
  const router = createMemoryRouter(
    [
      {
        path: "/spend",
        loader: () => data,
        Component: AdminSpend,
        HydrateFallback: () => null,
      },
      { path: "/users/:id", element: <p>user page</p> },
    ],
    { initialEntries: [url] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

describe("AdminSpend", () => {
  it("shows the window, month, today and call tiles, the model rows and the price versions", async () => {
    mount();
    expect(await screen.findByText("Last 30 days")).toBeDefined();
    expect(screen.getByText("This month")).toBeDefined();
    expect(screen.getByText("Today")).toBeDefined();
    expect(screen.getByText("40")).toBeDefined();
    expect(screen.getByText("anthropic:opus")).toBeDefined();
    expect(screen.getByText("12 calls")).toBeDefined();
    expect(screen.getByText("price book v7 · 40 calls")).toBeDefined();
    expect(screen.queryByText("Zero-cost calls")).toBeNull();
  });

  it("the total chart is a labelled bar chart with its peak", async () => {
    mount();
    expect(
      await screen.findByRole("img", { name: "Daily spend, 2026-11-01 to 2026-11-02" }),
    ).toBeDefined();
    expect(screen.getByText(/peak/)).toBeDefined();
  });

  it("a zero-cost call count raises the warning tile", async () => {
    mount({ summary: summary({ zero_cost_calls: 3 }), daily: totals });
    expect(await screen.findByText("Zero-cost calls")).toBeDefined();
    expect(screen.getByText("missing a price row")).toBeDefined();
  });

  it("a spender with an account links to it and one without is plain text", async () => {
    const user = userEvent.setup();
    const router = mount();
    const link = await screen.findByRole("link", { name: /ann@example.com/ });
    expect(screen.queryByRole("link", { name: /ghost@example.com/ })).toBeNull();
    await user.click(link);
    await waitFor(() => expect(router.state.location.pathname).toBe("/users/u1"));
  });

  it("By model fetches that grouping once, writes the URL and legends each key", async () => {
    const user = userEvent.setup();
    const router = mount();
    await user.click(await screen.findByRole("button", { name: "By model" }));
    expect(router.state.location.search).toBe("?by=model");
    expect(await screen.findByText(/busiest line/)).toBeDefined();
    expect(screen.getAllByText("opus").length).toBeGreaterThan(0);
    expect(screen.getAllByText("mini").length).toBeGreaterThan(0);
    const grouped = () =>
      get.mock.calls.filter(
        (c: unknown[]) =>
          (c[1] as { params: { query: { group?: string } } }).params.query.group === "model",
      );
    expect(grouped()).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Total" }));
    await user.click(screen.getByRole("button", { name: "By model" }));
    expect(grouped()).toHaveLength(1);
  });

  it("a deep link to a grouping opens on it, and an empty window says so", async () => {
    get.mockImplementation((() => ok([])) as never);
    mount({ summary: summary(), daily: [] }, "/spend?by=user");
    expect(await screen.findByText("No spend in the window")).toBeDefined();
  });

  it("a failed grouping fetch falls back to the empty state, not a spinner", async () => {
    const user = userEvent.setup();
    get.mockImplementation((() =>
      Promise.resolve({
        data: undefined,
        error: { error: { code: "x", message: "no" } },
        response: new Response(null, { status: 500 }),
      })) as never);
    mount();
    await user.click(await screen.findByRole("button", { name: "By user" }));
    expect(await screen.findByText("No spend in the window")).toBeDefined();
  });
});
