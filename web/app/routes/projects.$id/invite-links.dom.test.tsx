/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// Invite links, driven: the list loads and shows each link's state, Create link mints one at
// the default role and expiry and shows the URL once, revoke deletes a live link, and a failed
// load offers a retry instead of claiming there are none.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import { InviteLinks } from "./invite-links";

const ok = (data: unknown) => ({
  data,
  error: undefined,
  response: new Response(null, { status: 200 }),
});
const link = (id: string, over: Record<string, unknown> = {}) => ({
  id,
  role: "editor",
  state: "live",
  expires_at: new Date(Date.now() + 3 * 86_400_000).toISOString(),
  ...over,
});

let get: ReturnType<typeof vi.spyOn>;
let post: ReturnType<typeof vi.spyOn>;
let del: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  get = vi.spyOn(api, "GET").mockImplementation((() => Promise.resolve(ok([]))) as never);
  post = vi
    .spyOn(api, "POST")
    .mockImplementation((() => Promise.resolve(ok({ code: "abc123" }))) as never);
  del = vi.spyOn(api, "DELETE").mockImplementation((() => Promise.resolve(ok({}))) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

describe("Invite links list", () => {
  it("says there are none once an empty list has loaded", async () => {
    render(<InviteLinks projectId="p1" />);
    expect(await screen.findByText(/No invite links yet/)).toBeTruthy();
  });

  it("shows each link's role and state, with revoke only on live ones", async () => {
    get.mockResolvedValue(
      ok([link("l1"), link("l2", { role: "viewer", state: "used_up" })]) as never,
    );
    render(<InviteLinks projectId="p1" />);
    expect(await screen.findByText("Live")).toBeTruthy();
    expect(screen.getByText("Used")).toBeTruthy();
    expect(screen.getByText("expires in 3d")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Revoke editor link" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Revoke viewer link" })).toBeNull();
  });

  it("revokes a live link and reloads the list", async () => {
    const user = userEvent.setup();
    get.mockResolvedValueOnce(ok([link("l1")]) as never);
    render(<InviteLinks projectId="p1" />);
    await user.click(await screen.findByRole("button", { name: "Revoke editor link" }));
    expect(del).toHaveBeenCalledWith("/v1/projects/{project_id}/invites/{project_invite_id}", {
      params: { path: { project_id: "p1", project_invite_id: "l1" } },
    });
    expect(await screen.findByText(/No invite links yet/)).toBeTruthy();
  });

  it("offers a retry when the first load fails", async () => {
    const user = userEvent.setup();
    get.mockResolvedValueOnce({
      data: undefined,
      error: { detail: "boom" },
      response: new Response(null, { status: 500 }),
    } as never);
    render(<InviteLinks projectId="p1" />);
    expect(await screen.findByText("Couldn't load the invite links.")).toBeTruthy();
    expect(screen.queryByText(/No invite links yet/)).toBeNull();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText(/No invite links yet/)).toBeTruthy();
    expect(get).toHaveBeenCalledTimes(2);
  });
});

describe("Creating a link", () => {
  it("mints at the default role and expiry, then shows the link once", async () => {
    const user = userEvent.setup();
    render(<InviteLinks projectId="p1" />);
    await user.click(await screen.findByRole("button", { name: "Create link" }));
    await user.click(await screen.findByRole("button", { name: "Create link", hidden: false }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/v1/projects/{project_id}/invites", {
        params: { path: { project_id: "p1" } },
        body: { role: "editor", expires_in_days: 7 },
      }),
    );
    const url = (await screen.findByRole("textbox", { name: "Invite link" })) as HTMLInputElement;
    expect(url.value).toBe(`${window.location.origin}/join/abc123`);
    expect(screen.getByText("This link is shown once.")).toBeTruthy();
  });
});
