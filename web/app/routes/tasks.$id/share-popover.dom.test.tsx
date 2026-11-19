/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The Share popover, driven: a Project task lists its owner and members and lets only the owner
// mint an invite link; a personal task offers the viewer-free Project list and moves only after
// a confirming word; failures say so.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toastManager } from "@prismoid/blocks/toast";
import { api } from "~/lib/api/client";
import { SharePopover } from "./share-popover";

const reply = (data: unknown) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });

const PROJECT = (role: string) => ({
  id: "p1",
  name: "Launch",
  role,
  owner: { name: "Ada", email: "ada@x.co" },
});
const MEMBERS = [{ actor_id: "b", name: "Bob", email: "bob@x.co", role: "editor" }];
const PROJECTS = {
  items: [
    { id: "p1", name: "Launch", role: "owner" },
    { id: "p2", name: "Watching", role: "viewer" },
    { id: "p3", name: "Docs", role: "editor" },
  ],
};

let get: ReturnType<typeof vi.spyOn>;
let post: ReturnType<typeof vi.spyOn>;
let toasts: ReturnType<typeof vi.spyOn>;
let role = "owner";

beforeEach(() => {
  role = "owner";
  get = vi.spyOn(api, "GET").mockImplementation(((path: string) => {
    if (path === "/v1/projects/{project_id}") return reply(PROJECT(role));
    if (path === "/v1/projects/{project_id}/members") return reply(MEMBERS);
    return reply(PROJECTS);
  }) as never);
  post = vi.spyOn(api, "POST").mockImplementation((() => reply({ code: "abc123" })) as never);
  toasts = vi.spyOn(toastManager, "add").mockReturnValue("id");
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

async function open(projectId: string | null) {
  const router = createMemoryRouter(
    [
      {
        path: "/",
        element: <SharePopover detail={{ id: "t1", project_id: projectId }} />,
        loader: () => null,
      },
    ],
    { initialEntries: ["/"] },
  );
  render(<RouterProvider router={router} />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Share this task" }));
  return user;
}

describe("SharePopover on a Project task", () => {
  it("fetches nothing until opened, then names the Project, its owner and members", async () => {
    await open("p1");
    expect(await screen.findByText("Everyone in Launch can see this")).toBeTruthy();
    expect(await screen.findByText("Bob")).toBeTruthy();
    expect(screen.getByText("Ada")).toBeTruthy();
    expect(get.mock.calls.map((c: unknown[]) => c[0])).toEqual(
      expect.arrayContaining(["/v1/projects/{project_id}", "/v1/projects/{project_id}/members"]),
    );
  });

  it("the owner mints an editor invite link and sees it", async () => {
    const user = await open("p1");
    await user.click(await screen.findByRole("button", { name: "Invite to Project" }));
    const link = await screen.findByRole("textbox", { name: "Invite link" });
    expect((link as HTMLInputElement).value).toBe(`${window.location.origin}/join/abc123`);
    expect(post.mock.calls[0][0]).toBe("/v1/projects/{project_id}/invites");
    expect(post.mock.calls[0][1].body).toEqual({ role: "editor" });
  });

  it("a failed invite toasts the server's words and keeps the button", async () => {
    post.mockImplementation((() =>
      Promise.resolve({
        data: undefined,
        error: { error: { code: "x", message: "Invites are off." } },
        response: new Response(null, { status: 403 }),
      })) as never);
    const user = await open("p1");
    await user.click(await screen.findByRole("button", { name: "Invite to Project" }));
    await waitFor(() =>
      expect(toasts.mock.calls.map((c: unknown[]) => (c[0] as { title: string }).title)).toEqual([
        "Invites are off.",
      ]),
    );
    expect(screen.getByRole("button", { name: "Invite to Project" })).toBeTruthy();
  });

  it("a member (not the owner) gets no invite button", async () => {
    role = "editor";
    await open("p1");
    await screen.findByText("Bob");
    expect(screen.queryByRole("button", { name: "Invite to Project" })).toBeNull();
  });

  it("says so when the Project cannot be loaded", async () => {
    get.mockImplementation(((path: string) =>
      path === "/v1/projects/{project_id}"
        ? Promise.reject(new Error("down"))
        : reply(MEMBERS)) as never);
    await open("p1");
    expect(await screen.findByText("Couldn't load this Project.")).toBeTruthy();
  });
});

describe("SharePopover on a personal task", () => {
  it("offers only the Projects the caller can file into", async () => {
    await open(null);
    expect(await screen.findByRole("button", { name: "Move to Launch" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Move to Docs" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Move to Watching" })).toBeNull();
  });

  it("asks before moving, and Cancel moves nothing", async () => {
    const user = await open(null);
    await user.click(await screen.findByRole("button", { name: "Move to Docs" }));
    expect(
      screen.getByText("Everyone in Docs will be able to see this task. Move it?"),
    ).toBeTruthy();
    expect(post).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Move to Docs" })).toBeTruthy();
    expect(post).not.toHaveBeenCalled();
  });

  it("confirming posts the move, says where it went and closes the popover", async () => {
    const user = await open(null);
    await user.click(await screen.findByRole("button", { name: "Move to Docs" }));
    await user.click(screen.getByRole("button", { name: "Move it to Docs" }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(post.mock.calls[0][0]).toBe("/v1/tasks/{task_id}/move");
    expect(post.mock.calls[0][1].body).toEqual({ project_id: "p3" });
    await waitFor(() =>
      expect(toasts.mock.calls.map((c: unknown[]) => (c[0] as { title: string }).title)).toEqual([
        "Moved to Docs",
      ]),
    );
    await waitFor(() => expect(screen.queryByText(/Everyone in Docs/)).toBeNull());
  });

  it("a refused move toasts and leaves the task where it was", async () => {
    post.mockImplementation((() =>
      Promise.resolve({
        data: undefined,
        error: { error: { code: "x", message: "Not allowed." } },
        response: new Response(null, { status: 403 }),
      })) as never);
    const user = await open(null);
    await user.click(await screen.findByRole("button", { name: "Move to Docs" }));
    await user.click(screen.getByRole("button", { name: "Move it to Docs" }));
    await waitFor(() =>
      expect(toasts.mock.calls.map((c: unknown[]) => (c[0] as { title: string }).title)).toEqual([
        "Not allowed.",
      ]),
    );
  });

  it("says so when there are no Projects", async () => {
    get.mockImplementation((() => reply({ items: [] })) as never);
    await open(null);
    expect(await screen.findByText("You have no Projects yet.")).toBeTruthy();
  });
});
