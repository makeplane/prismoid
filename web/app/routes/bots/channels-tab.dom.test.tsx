/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// A bot's Channels tab, driven: the list shows each door's status and limits, Add webhook mints
// one and shows its token once, rotate swaps the token, disable/enable hit the right verb, and
// remove deletes the door.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import { ChannelsTab } from "./channels-tab";

const ok = (data: unknown) => ({
  data,
  error: undefined,
  response: new Response(null, { status: 200 }),
});
const channel = (id: string, over: Record<string, unknown> = {}) => ({
  id,
  kind: "webhook",
  label: `Door ${id}`,
  status: "active",
  budget_usd_per_turn: "0.5",
  daily_message_cap: 100,
  input_mode: "data",
  last_message_at: null,
  last_error: null,
  token: null,
  ...over,
});

let rows: unknown[];
let post: ReturnType<typeof vi.spyOn>;
let del: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  rows = [];
  vi.spyOn(api, "GET").mockImplementation((() => Promise.resolve(ok(rows))) as never);
  post = vi.spyOn(api, "POST").mockImplementation((() => Promise.resolve(ok({}))) as never);
  del = vi.spyOn(api, "DELETE").mockImplementation((() => Promise.resolve(ok({}))) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

describe("Channels list", () => {
  it("invites the first webhook when there are none", async () => {
    render(<ChannelsTab name="scout" projectId={null} />);
    expect(await screen.findByText("No doors yet")).toBeTruthy();
  });

  it("shows each door's limits, mode and last error", async () => {
    rows = [
      channel("c1", {
        input_mode: "instruction",
        last_error: "bad signature",
        status: "broken",
      }),
    ];
    render(<ChannelsTab name="scout" projectId={null} />);
    expect(await screen.findByText("Door c1")).toBeTruthy();
    expect(screen.getByText(/\$0\.50\/turn/)).toBeTruthy();
    expect(screen.getByText(/100\/day/)).toBeTruthy();
    expect(screen.getByText(/messages steer/)).toBeTruthy();
    expect(screen.getByText("bad signature")).toBeTruthy();
    expect(screen.getByText("broken")).toBeTruthy();
  });
});

describe("Channel actions", () => {
  it("mints a webhook for the bot and shows its token once", async () => {
    const user = userEvent.setup();
    post.mockImplementationOnce((() => {
      rows = [channel("c9")];
      return Promise.resolve(ok(channel("c9", { token: "bc_secret" })));
    }) as never);
    render(<ChannelsTab name="scout" projectId="p1" />);
    await user.click(await screen.findByRole("button", { name: "Add webhook" }));
    expect(post).toHaveBeenCalledWith("/v1/bots/{name}/channels", {
      params: { path: { name: "scout" } },
      body: { kind: "webhook", project_id: "p1", label: "scout webhook" },
    });
    expect(await screen.findByText("bc_secret")).toBeTruthy();
    expect(screen.getByText(/\/v1\/channels\/c9\/messages/)).toBeTruthy();
    expect(await screen.findByText("Door c9")).toBeTruthy();
  });

  it("rotates a token and shows the new one", async () => {
    const user = userEvent.setup();
    rows = [channel("c1")];
    post.mockResolvedValueOnce(ok(channel("c1", { token: "bc_new" })) as never);
    render(<ChannelsTab name="scout" projectId={null} />);
    await user.click(await screen.findByRole("button", { name: "Rotate the token" }));
    expect(post).toHaveBeenCalledWith("/v1/channels/{channel_id}/token", {
      params: { path: { channel_id: "c1" } },
    });
    expect(await screen.findByText("bc_new")).toBeTruthy();
  });

  it("disables an active door and enables a disabled one", async () => {
    const user = userEvent.setup();
    rows = [channel("c1"), channel("c2", { status: "disabled" })];
    render(<ChannelsTab name="scout" projectId={null} />);
    await user.click(await screen.findByRole("button", { name: "Disable" }));
    expect(post).toHaveBeenCalledWith("/v1/channels/{channel_id}/disable", {
      params: { path: { channel_id: "c1" } },
    });
    await user.click(screen.getByRole("button", { name: "Enable" }));
    expect(post).toHaveBeenCalledWith("/v1/channels/{channel_id}/enable", {
      params: { path: { channel_id: "c2" } },
    });
  });

  it("removes a door and reloads the list", async () => {
    const user = userEvent.setup();
    rows = [channel("c1")];
    del.mockImplementationOnce((() => {
      rows = [];
      return Promise.resolve(ok({}));
    }) as never);
    render(<ChannelsTab name="scout" projectId={null} />);
    await user.click(await screen.findByRole("button", { name: "Remove the channel" }));
    expect(del).toHaveBeenCalledWith("/v1/channels/{channel_id}", {
      params: { path: { channel_id: "c1" } },
    });
    expect(await screen.findByText("No doors yet")).toBeTruthy();
  });

  it("removing a door takes its shown-once token away with it", async () => {
    const user = userEvent.setup();
    rows = [channel("c1"), channel("c2")];
    post.mockResolvedValueOnce(ok(channel("c1", { token: "bc_new" })) as never);
    del.mockImplementationOnce((() => {
      rows = [channel("c2")];
      return Promise.resolve(ok({}));
    }) as never);
    render(<ChannelsTab name="scout" projectId={null} />);
    await user.click((await screen.findAllByRole("button", { name: "Rotate the token" }))[0]);
    expect(await screen.findByText("bc_new")).toBeTruthy();
    await user.click(screen.getAllByRole("button", { name: "Remove the channel" })[0]);
    await waitFor(() => expect(screen.queryByText("Door c1")).toBeNull());
    expect(screen.queryByText("bc_new")).toBeNull();
  });
});
