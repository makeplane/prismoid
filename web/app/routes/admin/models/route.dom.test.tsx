/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// Model administration: doctor findings, the catalog's enable switches, the per-model reprice
// dialog and the add-model dialog each send the exact admin wire body (prices stay strings,
// reprice names only its own ref), and the routing table lists unrouted classes honestly.
import { toast } from "@prismoid/blocks/toast";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import AdminModels from "./route";

const ok = (data: unknown) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });

const model = (ref: string, over: Record<string, unknown> = {}) => ({
  ref,
  family: "claude",
  enabled: true,
  classes: ["chat"],
  context_window: 200000,
  ...over,
});

type Data = {
  models: unknown[];
  routing: { chains: Record<string, string[]>; classes: string[]; version: number };
  doctor: { check: string; ref: string; detail: string }[];
  prices: { version: number; prices: Record<string, Record<string, string>> } | null;
};

const full: Data = {
  models: [model("anthropic/opus"), model("openai/mini", { enabled: false, family: "gpt" })],
  routing: {
    chains: { chat: ["anthropic/opus", "openai/mini"] },
    classes: ["chat", "image-gen"],
    version: 4,
  },
  doctor: [],
  prices: {
    version: 7,
    prices: { "anthropic/opus": { input_token: "0.000003", output_token: "0.000015" } },
  },
};

let patch: ReturnType<typeof vi.spyOn>;
let put: ReturnType<typeof vi.spyOn>;
let post: ReturnType<typeof vi.spyOn>;
let loads: number;
beforeEach(() => {
  patch = vi.spyOn(api, "PATCH").mockImplementation((() => ok({})) as never);
  put = vi.spyOn(api, "PUT").mockImplementation((() => ok({ version: 8 })) as never);
  post = vi.spyOn(api, "POST").mockImplementation((() => ok({})) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

function mount(data: Data = full) {
  loads = 0;
  const router = createMemoryRouter(
    [
      {
        path: "/",
        loader: () => {
          loads += 1;
          return data;
        },
        Component: AdminModels,
        HydrateFallback: () => null,
      },
    ],
    { initialEntries: ["/"] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

describe("AdminModels read side", () => {
  it("healthy catalog, models, price book and routing chains render", async () => {
    mount();
    expect(await screen.findByText("Catalog healthy")).toBeDefined();
    expect(screen.getAllByText("anthropic/opus").length).toBeGreaterThan(0);
    expect(screen.getAllByText("200K ctx")).toHaveLength(2);
    expect(screen.getByText("Price book · v7")).toBeDefined();
    expect(screen.getByText("input_token = 0.000003")).toBeDefined();
    expect(screen.getByText("Routing table · v4")).toBeDefined();
    expect(screen.getByText("anthropic/opus → openai/mini")).toBeDefined();
    expect(screen.getByText(/unrouted - falls back/)).toBeDefined();
  });

  it("doctor findings lead the page and a missing price book says so", async () => {
    mount({
      ...full,
      models: [],
      prices: null,
      doctor: [{ check: "no_price", ref: "openai/mini", detail: "Enabled with no price." }],
    });
    expect(await screen.findByText("Enabled with no price.")).toBeDefined();
    expect(screen.getByText("no_price")).toBeDefined();
    expect(screen.getByText("No models yet")).toBeDefined();
    expect(screen.getByText("No prices yet")).toBeDefined();
  });
});

describe("AdminModels switches", () => {
  it("a switch PATCHes that model's enabled flag and the page reloads", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("switch", { name: "Enable openai/mini" }));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(patch.mock.calls[0]?.[0]).toBe("/v1/admin/models/{id}");
    expect(patch.mock.calls[0]?.[1]).toEqual({
      params: { path: { id: "openai/mini" } },
      body: { enabled: true },
    });
    await waitFor(() => expect(loads).toBe(2));
  });

  it("a failed toggle toasts the server's words", async () => {
    const user = userEvent.setup();
    const error = vi.spyOn(toast, "error");
    patch.mockImplementation((() =>
      Promise.resolve({
        data: undefined,
        error: { error: { code: "x", message: "Needs a price first." } },
        response: new Response(null, { status: 409 }),
      })) as never);
    mount();
    await user.click(await screen.findByRole("switch", { name: "Disable anthropic/opus" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("Needs a price first."));
  });
});

describe("AdminModels reprice", () => {
  it("Save stays off until a price changes, then PUTs only that ref with string prices", async () => {
    const user = userEvent.setup();
    const success = vi.spyOn(toast, "success");
    mount();
    await user.click(await screen.findByRole("button", { name: "Edit prices" }));
    const save = await screen.findByRole("button", { name: "Save new version" });
    expect(save.hasAttribute("disabled")).toBe(true);
    const out = screen.getByRole("textbox", { name: "output_token" });
    fireEvent.change(out, { target: { value: "0.00002" } });
    await user.click(save);
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(put.mock.calls[0]?.[1]).toEqual({
      body: {
        prices: { "anthropic/opus": { input_token: "0.000003", output_token: "0.00002" } },
      },
    });
    expect(success).toHaveBeenCalledWith("Price book v8 - anthropic/opus repriced.");
    await waitFor(() => expect(loads).toBe(2));
  });
});

// One change event per field: these inputs read currentTarget inside a state updater.
const fill = async (name: string, value: string) => {
  fireEvent.change(screen.getByRole("textbox", { name }), { target: { value } });
  await act(async () => {});
};

describe("AdminModels add model", () => {
  async function openAdd(user: ReturnType<typeof userEvent.setup>) {
    mount();
    await user.click(await screen.findByRole("button", { name: "Add model" }));
    await screen.findByRole("dialog");
  }

  it("refuses an incomplete form without calling the wire", async () => {
    const user = userEvent.setup();
    const error = vi.spyOn(toast, "error");
    await openAdd(user);
    await user.click(screen.getByRole("button", { name: "Add model" }));
    expect(error).toHaveBeenCalledWith(
      "Provider, model id, family, and at least one class are required.",
    );
    expect(post).not.toHaveBeenCalled();
  });

  it("sends the form, the chosen classes and no blank prices", async () => {
    const user = userEvent.setup();
    const success = vi.spyOn(toast, "success");
    await openAdd(user);
    await fill("Provider", "anthropic");
    await fill("Model id", "claude-x");
    await fill("Family", "claude");
    await user.click(screen.getByRole("button", { name: "chat" }));
    await user.click(screen.getByRole("button", { name: "Add model" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post.mock.calls[0]?.[0]).toBe("/v1/admin/models");
    expect(post.mock.calls[0]?.[1]).toEqual({
      body: {
        provider: "anthropic",
        model_id: "claude-x",
        family: "claude",
        protocol: "anthropic",
        context_window: 200000,
        classes: ["chat"],
        prices: {},
      },
    });
    expect(success).toHaveBeenCalledWith(expect.stringContaining("price it before you enable it"));
    await waitFor(() => expect(loads).toBe(2));
  });

  it("choosing image-gen swaps the price fields to the image unit, and unpriced warns", async () => {
    const user = userEvent.setup();
    const success = vi.spyOn(toast, "success");
    await openAdd(user);
    await fill("Provider", "p");
    await fill("Model id", "m");
    await fill("Family", "f");
    await user.click(screen.getByRole("button", { name: "image-gen" }));
    expect(screen.getByRole("textbox", { name: "image" })).toBeDefined();
    expect(screen.queryByRole("textbox", { name: "input_token" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Add model" }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(success).toHaveBeenCalledWith(expect.stringContaining("meters at $0"));
  });
});
