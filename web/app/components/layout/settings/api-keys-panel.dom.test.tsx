/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The API keys panel: keys list with their usage, Revoke deletes by id and reloads, a new key is
// named, created and shown once with a Done to close, and a failed load or create says so.
import { toast } from "@prismoid/blocks/toast";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import { ApiKeysPanel } from "./api-keys-panel";

const ok = (data: unknown) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });

const key = (id: string, over: Record<string, unknown> = {}) => ({
  id,
  name: `key-${id}`,
  created_at: "2026-11-01T10:00:00Z",
  last_used_at: null,
  revoked_at: null,
  ...over,
});

let keys: unknown[];
let get: ReturnType<typeof vi.spyOn>;
let post: ReturnType<typeof vi.spyOn>;
let del: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  keys = [
    key("a"),
    key("b", { last_used_at: "2026-11-02T10:00:00Z" }),
    key("c", { last_used_at: "2026-11-02T10:00:00Z", revoked_at: "2026-11-03T00:00:00Z" }),
  ];
  get = vi.spyOn(api, "GET").mockImplementation((() => ok(keys)) as never);
  post = vi.spyOn(api, "POST").mockImplementation((() => ok({ key: "ck_secret_value" })) as never);
  del = vi.spyOn(api, "DELETE").mockImplementation((() => ok({})) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

describe("ApiKeysPanel", () => {
  it("lists keys with usage, marks a revoked one and offers Revoke only on live keys", async () => {
    render(<ApiKeysPanel />);
    expect(await screen.findByText("key-a")).toBeDefined();
    expect(screen.getByText(/never used/)).toBeDefined();
    expect(screen.getAllByText(/last used/)).toBeDefined();
    expect(screen.getByText("revoked")).toBeDefined();
    expect(screen.getAllByRole("button", { name: "Revoke" })).toHaveLength(2);
  });

  it("no keys says how to get one", async () => {
    keys = [];
    render(<ApiKeysPanel />);
    expect(await screen.findByText("No API keys yet")).toBeDefined();
  });

  it("a failed load toasts", async () => {
    const error = vi.spyOn(toast, "error");
    get.mockImplementation((() =>
      Promise.resolve({
        data: undefined,
        error: { error: { code: "x", message: "no" } },
        response: new Response(null, { status: 500 }),
      })) as never);
    render(<ApiKeysPanel />);
    await waitFor(() => expect(error).toHaveBeenCalledWith("Couldn't load API keys."));
  });

  it("Revoke deletes that key by id and reloads the list", async () => {
    const user = userEvent.setup();
    render(<ApiKeysPanel />);
    await screen.findByText("key-a");
    await user.click(screen.getAllByRole("button", { name: "Revoke" })[0]!);
    await waitFor(() => expect(del).toHaveBeenCalledTimes(1));
    expect(del.mock.calls[0]?.[0]).toBe("/v1/auth/api-keys/{key_id}");
    expect(del.mock.calls[0]?.[1]).toEqual({ params: { path: { key_id: "a" } } });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  });

  it("a new key is named, created, shown once and closed with Done", async () => {
    const user = userEvent.setup();
    render(<ApiKeysPanel />);
    await user.click(await screen.findByRole("button", { name: "New key" }));
    const create = await screen.findByRole("button", { name: "Create key" });
    expect(create.hasAttribute("disabled")).toBe(true);
    await user.type(screen.getByRole("textbox", { name: "Name" }), "  laptop CLI ");
    await user.click(create);
    expect(await screen.findByText("ck_secret_value")).toBeDefined();
    expect(post.mock.calls[0]?.[1]).toEqual({ body: { name: "laptop CLI" } });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    await user.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.queryByText("ck_secret_value")).toBeNull());
  });

  it("a failed create toasts and keeps the naming step", async () => {
    const user = userEvent.setup();
    const error = vi.spyOn(toast, "error");
    post.mockImplementation((() =>
      Promise.resolve({
        data: undefined,
        error: { error: { code: "x", message: "Key limit reached." } },
        response: new Response(null, { status: 409 }),
      })) as never);
    render(<ApiKeysPanel />);
    await user.click(await screen.findByRole("button", { name: "New key" }));
    await user.type(await screen.findByRole("textbox", { name: "Name" }), "k");
    await user.click(screen.getByRole("button", { name: "Create key" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("Key limit reached."));
    expect(screen.getByRole("textbox", { name: "Name" })).toBeDefined();
  });
});
