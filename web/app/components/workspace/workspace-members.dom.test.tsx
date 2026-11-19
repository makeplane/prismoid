/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The Members panel's wiring: it asks who the caller is, loads the roster (and the pending
// invites only for someone who may manage them), and each action calls its own door, says so,
// and reloads the roster. A failed load or action toasts instead of breaking the panel.
import { toast } from "@prismoid/blocks/toast";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import { WorkspaceMembersPanel } from "./workspace-members";

const reply = (status: number, data: unknown, error?: unknown) =>
  Promise.resolve({ data, error, response: new Response(null, { status }) });
const failure = (status: number, message: string) =>
  reply(status, undefined, { error: { code: "x", message } });

const member = (id: string, role: string) => ({
  user: { id, email: `${id}@example.com`, name: id === "me" ? "Me Myself" : `Person ${id}` },
  role,
  joined_at: "2026-09-01T00:00:00Z",
});
const invite = {
  id: "i1",
  email: "new@example.com",
  role: "member",
  expires_at: "2099-01-01T00:00:00Z",
  invited_by: null,
};

let myRole: string;
let get: ReturnType<typeof vi.spyOn>;
let post: ReturnType<typeof vi.spyOn>;
let del: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  myRole = "owner";
  get = vi.spyOn(api, "GET").mockImplementation(((path: string) => {
    if (path === "/v1/auth/me")
      return reply(200, {
        id: "me",
        email: "me@example.com",
        name: "Me Myself",
        workspace: { id: "w1", name: "Acme", kind: "team", role: myRole },
      });
    if (path === "/v1/workspaces/{workspace_id}/members")
      return reply(200, [member("me", myRole), member("bob", "member")]);
    if (path === "/v1/workspaces/{workspace_id}/invites") return reply(200, [invite]);
    return reply(404, undefined, { error: { code: "not_found", message: path } });
  }) as never);
  post = vi.spyOn(api, "POST").mockImplementation((() => reply(200, {})) as never);
  del = vi.spyOn(api, "DELETE").mockImplementation((() => reply(204, undefined)) as never);
  vi.spyOn(toast, "success").mockImplementation((() => "") as never);
  vi.spyOn(toast, "error").mockImplementation((() => "") as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

const calls = (spy: ReturnType<typeof vi.spyOn>, path: string) =>
  spy.mock.calls.filter((c: unknown[]) => c[0] === path);

describe("WorkspaceMembersPanel", () => {
  it("an owner sees the roster and the pending invites", async () => {
    render(<WorkspaceMembersPanel />);
    expect(await screen.findByText("Person bob")).toBeDefined();
    expect(screen.getByText("Me Myself")).toBeDefined();
    expect(await screen.findByText("new@example.com")).toBeDefined();
    expect(calls(get, "/v1/workspaces/{workspace_id}/members")[0]?.[1]).toEqual({
      params: { path: { workspace_id: "w1" } },
    });
  });

  it("a member is never asked for the invites", async () => {
    myRole = "member";
    render(<WorkspaceMembersPanel />);
    expect(await screen.findByText("Person bob")).toBeDefined();
    expect(calls(get, "/v1/workspaces/{workspace_id}/invites")).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "Invite" })).toBeNull();
  });

  it("sends an invite, says so and reloads the roster", async () => {
    const user = userEvent.setup();
    render(<WorkspaceMembersPanel />);
    await screen.findByText("Person bob");
    await user.click(screen.getByRole("button", { name: "Invite" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: "Email" }), " ada@example.com ");
    await user.click(within(dialog).getByRole("button", { name: "Send invite" }));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Invite sent."));
    expect(calls(post, "/v1/workspaces/{workspace_id}/invites")[0]?.[1]).toEqual({
      params: { path: { workspace_id: "w1" } },
      body: { email: "ada@example.com", role: "member" },
    });
    await waitFor(() =>
      expect(calls(get, "/v1/workspaces/{workspace_id}/members").length).toBeGreaterThan(1),
    );
  });

  it("revokes a pending invite", async () => {
    const user = userEvent.setup();
    render(<WorkspaceMembersPanel />);
    await screen.findByText("new@example.com");
    await user.click(screen.getByRole("button", { name: "Revoke" }));
    await waitFor(() =>
      expect(calls(del, "/v1/workspaces/{workspace_id}/invites/{invite_id}")[0]?.[1]).toEqual({
        params: { path: { workspace_id: "w1", invite_id: "i1" } },
      }),
    );
  });

  it("removes a member after the confirm", async () => {
    const user = userEvent.setup();
    render(<WorkspaceMembersPanel />);
    await screen.findByText("Person bob");
    await user.click(screen.getByRole("button", { name: "Remove" }));
    const confirm = await screen.findByRole("alertdialog");
    expect(within(confirm).getByText("Remove bob@example.com?")).toBeDefined();
    await user.click(within(confirm).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Removed."));
    expect(calls(del, "/v1/workspaces/{workspace_id}/members/{actor_id}")[0]?.[1]).toEqual({
      params: { path: { workspace_id: "w1", actor_id: "bob" } },
    });
  });

  it("transfers ownership after the confirm", async () => {
    const user = userEvent.setup();
    render(<WorkspaceMembersPanel />);
    await screen.findByText("Person bob");
    await user.click(screen.getByRole("button", { name: "Transfer" }));
    const confirm = await screen.findByRole("alertdialog");
    await user.click(within(confirm).getByRole("button", { name: "Transfer" }));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Ownership transferred."));
    expect(calls(post, "/v1/workspaces/{workspace_id}/transfer")[0]?.[1]).toEqual({
      params: { path: { workspace_id: "w1" } },
      body: { actor_id: "bob" },
    });
  });

  it("a failed action toasts its own words", async () => {
    del.mockImplementation((() => failure(409, "Not allowed.")) as never);
    const user = userEvent.setup();
    render(<WorkspaceMembersPanel />);
    await screen.findByText("Person bob");
    await user.click(screen.getByRole("button", { name: "Remove" }));
    const confirm = await screen.findByRole("alertdialog");
    await user.click(within(confirm).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("a roster that fails to load says so", async () => {
    get.mockImplementation(((path: string) =>
      path === "/v1/auth/me"
        ? reply(200, {
            id: "me",
            workspace: { id: "w1", name: "Acme", kind: "team", role: "owner" },
          })
        : failure(500, "boom")) as never);
    render(<WorkspaceMembersPanel />);
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Couldn't load members."));
  });
});
