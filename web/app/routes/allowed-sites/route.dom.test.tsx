/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// Allowed sites as a page: adding a host posts it trimmed and shows it first, a 422 puts the
// server's words under the field, a failed add toasts, and Revoke removes the row by id.
import { toast } from "@prismoid/blocks/toast";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import AllowedSites from "./route";

type Props = ComponentProps<typeof AllowedSites>;
type Row = Props["loaderData"]["rows"][number];

const row = (id: string, host: string, source = "settings"): Row => ({
  id,
  host,
  source,
  gate_id: null,
  created_at: "2026-11-01T10:00:00Z",
  revoked_at: null,
});

const reply = (status: number, data: unknown, error?: unknown) =>
  Promise.resolve({ data, error, response: new Response(null, { status }) });

let post: ReturnType<typeof vi.spyOn>;
let del: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  post = vi
    .spyOn(api, "POST")
    .mockImplementation((() => reply(200, row("n", "new.example.com"))) as never);
  del = vi.spyOn(api, "DELETE").mockImplementation((() => reply(200, {})) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

function mount(rows: Row[]) {
  const router = createMemoryRouter(
    [{ path: "/", element: <AllowedSites {...({ loaderData: { rows } } as unknown as Props)} /> }],
    { initialEntries: ["/"] },
  );
  render(<RouterProvider router={router} />);
}

describe("AllowedSites", () => {
  it("Always allow stays off until a host is typed, then posts it trimmed and lists it first", async () => {
    const user = userEvent.setup();
    mount([row("1", "docs.vendor.com")]);
    const add = screen.getByRole("button", { name: "Always allow" });
    expect(add.hasAttribute("disabled")).toBe(true);
    await user.type(screen.getByRole("textbox", { name: "Host" }), "  new.example.com ");
    await user.click(add);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post.mock.calls[0]?.[1]).toEqual({ body: { host: "new.example.com" } });
    const hosts = (await screen.findAllByText(/\.(example|vendor)\.com$|^new\.example\.com$/)).map(
      (e) => e.textContent,
    );
    expect(hosts[0]).toBe("new.example.com");
    expect((screen.getByRole("textbox", { name: "Host" }) as HTMLInputElement).value).toBe("");
  });

  it("a 422 puts the server's words under the field and typing clears them", async () => {
    const user = userEvent.setup();
    post.mockImplementation((() =>
      reply(422, undefined, {
        error: { code: "invalid", message: "bad", fields: { host: "That is not a hostname." } },
      })) as never);
    mount([]);
    await user.type(screen.getByRole("textbox", { name: "Host" }), "???");
    await user.click(screen.getByRole("button", { name: "Always allow" }));
    expect(await screen.findByText("That is not a hostname.")).toBeDefined();
    await user.type(screen.getByRole("textbox", { name: "Host" }), "x");
    expect(screen.queryByText("That is not a hostname.")).toBeNull();
  });

  it("any other failure toasts instead", async () => {
    const user = userEvent.setup();
    const error = vi.spyOn(toast, "error");
    post.mockImplementation((() =>
      reply(500, undefined, { error: { code: "x", message: "Server is down." } })) as never);
    mount([]);
    await user.type(screen.getByRole("textbox", { name: "Host" }), "a.com");
    await user.click(screen.getByRole("button", { name: "Always allow" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("Server is down."));
  });

  it("Revoke deletes by id and drops the row", async () => {
    const user = userEvent.setup();
    mount([row("1", "docs.vendor.com"), row("2", "admin.example.com", "gate")]);
    await user.click(screen.getAllByRole("button", { name: "Revoke" })[0]!);
    await waitFor(() => expect(del).toHaveBeenCalledTimes(1));
    expect(del.mock.calls[0]?.[1]).toEqual({ params: { path: { allowance_id: "1" } } });
    await waitFor(() => expect(screen.queryByText("docs.vendor.com")).toBeNull());
    expect(screen.getByText("admin.example.com")).toBeDefined();
  });

  it("a failed revoke toasts and keeps the row", async () => {
    const user = userEvent.setup();
    const error = vi.spyOn(toast, "error");
    del.mockImplementation((() =>
      reply(500, undefined, { error: { code: "x", message: "Couldn't do it." } })) as never);
    mount([row("1", "docs.vendor.com")]);
    await user.click(screen.getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("Couldn't do it."));
    expect(screen.getByText("docs.vendor.com")).toBeDefined();
  });
});
