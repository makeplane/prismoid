/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The settings connectors roster: rows show status and the heal verbs that fit (Connect for
// OAuth, Retry for others), Retry probes and reports, Remove deletes by id, Connect starts the
// consent flow with a Cancel, and an empty or failed load says so with a way to retry.
import { toast } from "@prismoid/blocks/toast";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ConnectorRow } from "~/components/connectors/vocabulary";
import { api } from "~/lib/api/client";
import { ConnectorsPanel } from "./connectors-panel";

const reply = (status: number, data: unknown, error?: unknown) =>
  Promise.resolve({ data, error, response: new Response(null, { status }) });

const row = (id: string, over: Partial<ConnectorRow> = {}): ConnectorRow =>
  ({
    id,
    name: `Svc ${id}`,
    slug: id,
    server_url: `https://${id}.example/mcp`,
    status: "live",
    auth_kind: "oauth",
    last_error: null,
    tools: [],
    catalog_slug: null,
    ...over,
  }) as ConnectorRow;

let rows: ConnectorRow[];
let get: ReturnType<typeof vi.spyOn>;
let post: ReturnType<typeof vi.spyOn>;
let del: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  rows = [
    row("a", { status: "pending_auth" }),
    row("b", { status: "error", auth_kind: "none", last_error: "timeout" }),
  ];
  get = vi.spyOn(api, "GET").mockImplementation((() => reply(200, { items: rows })) as never);
  post = vi.spyOn(api, "POST").mockImplementation((() => reply(200, {})) as never);
  del = vi.spyOn(api, "DELETE").mockImplementation((() => reply(200, {})) as never);
  vi.spyOn(window, "open").mockImplementation((() => null) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

const mount = () =>
  render(
    <MemoryRouter>
      <ConnectorsPanel />
    </MemoryRouter>,
  );

describe("ConnectorsPanel", () => {
  it("shows each row with its status, the error as its subtitle, and the verb that fits", async () => {
    mount();
    expect(await screen.findByText("Svc a")).toBeDefined();
    expect(screen.getByText("Needs authorization")).toBeDefined();
    expect(screen.getByText("timeout")).toBeDefined();
    expect(screen.getByRole("button", { name: "Connect" })).toBeDefined();
    expect(screen.getByRole("button", { name: "Retry" })).toBeDefined();
    expect(screen.getByText("Open connectors").closest("a")?.getAttribute("href")).toBe(
      "/connectors",
    );
  });

  it("Retry probes the row, folds the answer in and toasts the verdict", async () => {
    const user = userEvent.setup();
    const success = vi.spyOn(toast, "success");
    post.mockImplementation((() =>
      reply(200, row("b", { status: "live", auth_kind: "none" }))) as never);
    mount();
    await user.click(await screen.findByRole("button", { name: "Retry" }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("Svc b connected."));
    expect(post.mock.calls[0]?.[0]).toBe("/v1/connectors/{connector_id}/probe");
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("a probe that still fails toasts the row's error", async () => {
    const user = userEvent.setup();
    const error = vi.spyOn(toast, "error");
    post.mockImplementation((() =>
      reply(
        200,
        row("b", { status: "error", auth_kind: "none", last_error: "still down" }),
      )) as never);
    mount();
    await user.click(await screen.findByRole("button", { name: "Retry" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("still down"));
  });

  it("Remove deletes by id and the row leaves the list", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "Remove Svc a" }));
    await waitFor(() => expect(del).toHaveBeenCalledTimes(1));
    expect(del.mock.calls[0]?.[1]).toEqual({ params: { path: { connector_id: "a" } } });
    await waitFor(() => expect(screen.queryByText("Svc a")).toBeNull());
  });

  it("Connect starts the consent flow, offers a link when the popup was blocked, and Cancel ends it", async () => {
    const user = userEvent.setup();
    post.mockImplementation((() => reply(200, { authorize_url: "https://idp/c" })) as never);
    mount();
    await user.click(await screen.findByRole("button", { name: "Connect" }));
    expect(await screen.findByText("Open authorization page")).toBeDefined();
    expect(screen.getByText("Waiting for the provider…")).toBeDefined();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByText("Waiting for the provider…")).toBeNull();
  });

  it("an empty roster points at the page; a failed load offers Try again", async () => {
    rows = [];
    mount();
    expect(await screen.findByText("No connectors yet")).toBeDefined();
    cleanup();
    get.mockImplementationOnce((() =>
      reply(500, undefined, { error: { code: "x", message: "no" } })) as never);
    mount();
    const again = await screen.findByRole("button", { name: "Try again" });
    await userEvent.setup().click(again);
    expect(await screen.findByText("No connectors yet")).toBeDefined();
  });
});
