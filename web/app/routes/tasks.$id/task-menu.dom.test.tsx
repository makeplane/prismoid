/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The task page's actions menu, driven: it offers only the verbs the caller may use, control
// items call back, rename saves a trimmed label, archive toggles, delete confirms then leaves
// the page, and a live task's archive and delete stay disabled.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toastManager } from "@prismoid/blocks/toast";
import { api } from "~/lib/api/client";
import { TaskMenu, type TaskAccess } from "./task-menu";

const win = vi.hoisted(() => ({ remove: vi.fn() }));
vi.mock("~/components/tasks/task-window", () => ({ useTaskWindow: () => win }));

const FULL: TaskAccess = { canEdit: true, canCurate: true, isCreator: true };
const ok = () =>
  Promise.resolve({ data: {}, error: undefined, response: new Response(null, { status: 200 }) });

let patch: ReturnType<typeof vi.spyOn>;
let post: ReturnType<typeof vi.spyOn>;
let toasts: ReturnType<typeof vi.spyOn>;
const onControl = vi.fn();
const onFork = vi.fn();
let loads = 0;

function mount(props: Partial<React.ComponentProps<typeof TaskMenu>> = {}) {
  const router = createMemoryRouter(
    [
      {
        path: "/task",
        loader: () => ++loads,
        element: (
          <TaskMenu
            taskId="t1"
            title="Quarterly plan"
            goal="plan the quarter"
            status="delivered"
            access={FULL}
            onControl={onControl}
            onFork={onFork}
            {...props}
          />
        ),
      },
      { path: "/tasks", element: <p>task list</p> },
    ],
    { initialEntries: ["/task"] },
  );
  render(<RouterProvider router={router} />);
  return { router, user: userEvent.setup() };
}

const openMenu = async (user: ReturnType<typeof userEvent.setup>) =>
  user.click(await screen.findByRole("button", { name: "Task actions" }));

beforeEach(() => {
  loads = 0;
  patch = vi.spyOn(api, "PATCH").mockImplementation((() => ok()) as never);
  post = vi.spyOn(api, "POST").mockImplementation((() => ok()) as never);
  toasts = vi.spyOn(toastManager, "add").mockReturnValue("id");
  win.remove.mockResolvedValue(true);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  win.remove.mockReset();
  onControl.mockReset();
  onFork.mockReset();
  document.body.innerHTML = "";
});

describe("TaskMenu verbs", () => {
  it("renders nothing for a viewer with no verbs", () => {
    mount({ access: { canEdit: false, canCurate: false, isCreator: false } });
    expect(screen.queryByRole("button", { name: "Task actions" })).toBeNull();
  });

  it("a live task shows Pause and Cancel, a paused one Resume", async () => {
    const { user } = mount({ status: "running" });
    await openMenu(user);
    await user.click(await screen.findByRole("menuitem", { name: "Pause" }));
    expect(onControl).toHaveBeenCalledWith("pause");
    cleanup();
    const again = mount({ status: "paused" });
    await openMenu(again.user);
    await again.user.click(await screen.findByRole("menuitem", { name: "Resume" }));
    expect(onControl).toHaveBeenLastCalledWith("resume");
  });

  it("Cancel task reports the cancel intent", async () => {
    const { user } = mount({ status: "running" });
    await openMenu(user);
    await user.click(await screen.findByRole("menuitem", { name: "Cancel task" }));
    expect(onControl).toHaveBeenCalledWith("cancel");
  });

  it("a settled task has no control items, and Fork this task calls back", async () => {
    const { user } = mount();
    await openMenu(user);
    expect(screen.queryByRole("menuitem", { name: "Pause" })).toBeNull();
    await user.click(await screen.findByRole("menuitem", { name: "Fork this task" }));
    expect(onFork).toHaveBeenCalled();
  });

  it("a non-creator does not get Rename", async () => {
    const { user } = mount({ access: { ...FULL, isCreator: false } });
    await openMenu(user);
    expect(screen.queryByRole("menuitem", { name: "Rename" })).toBeNull();
    expect(screen.getByRole("menuitem", { name: "Archive" })).toBeTruthy();
  });

  it("archive and delete are disabled while the task is live", async () => {
    const { user } = mount({ status: "running" });
    await openMenu(user);
    expect(screen.getByRole("menuitem", { name: "Archive" }).getAttribute("aria-disabled")).toBe(
      "true",
    );
    expect(screen.getByRole("menuitem", { name: "Delete" }).getAttribute("aria-disabled")).toBe(
      "true",
    );
  });
});

describe("TaskMenu archive", () => {
  it("archives a settled task and re-reads the page", async () => {
    const { user } = mount();
    await openMenu(user);
    await user.click(await screen.findByRole("menuitem", { name: "Archive" }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(post.mock.calls[0][0]).toBe("/v1/tasks/{task_id}/archive");
    await waitFor(() => expect(loads).toBeGreaterThan(1));
  });

  it("an archived task offers Unarchive", async () => {
    const { user } = mount({ archivedAt: "2026-11-01T00:00:00Z" });
    await openMenu(user);
    await user.click(await screen.findByRole("menuitem", { name: "Unarchive" }));
    await waitFor(() => expect(post.mock.calls[0][0]).toBe("/v1/tasks/{task_id}/unarchive"));
  });

  it("a failed archive toasts the server's words", async () => {
    post.mockImplementation((() =>
      Promise.resolve({
        data: undefined,
        error: { error: { code: "x", message: "Still running." } },
        response: new Response(null, { status: 409 }),
      })) as never);
    const { user } = mount();
    await openMenu(user);
    await user.click(await screen.findByRole("menuitem", { name: "Archive" }));
    await waitFor(() =>
      expect(toasts.mock.calls.map((c: unknown[]) => (c[0] as { title: string }).title)).toEqual([
        "Still running.",
      ]),
    );
  });
});

describe("TaskMenu rename", () => {
  async function openRename(user: ReturnType<typeof userEvent.setup>) {
    await openMenu(user);
    await user.click(await screen.findByRole("menuitem", { name: "Rename" }));
    return screen.findByRole("textbox", { name: "Task name" });
  }

  it("starts from the current title and saves a trimmed new one, then re-reads", async () => {
    const { user } = mount();
    const box = await openRename(user);
    expect((box as HTMLInputElement).value).toBe("Quarterly plan");
    await user.clear(box);
    await user.type(box, "  Roadmap  ");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patch).toHaveBeenCalled());
    expect(patch.mock.calls[0][1].body).toEqual({ title: "Roadmap" });
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Task name" })).toBeNull());
    expect(loads).toBeGreaterThan(1);
  });

  it("Enter saves, and an unchanged name closes without a request", async () => {
    const { user } = mount();
    const box = await openRename(user);
    await user.type(box, "{Enter}");
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Task name" })).toBeNull());
    expect(patch).not.toHaveBeenCalled();
  });

  it("Escape closes without saving, and a blank name cannot be saved", async () => {
    const { user } = mount();
    const box = await openRename(user);
    await user.clear(box);
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    await user.type(box, "{Escape}");
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Task name" })).toBeNull());
    expect(patch).not.toHaveBeenCalled();
  });

  it("a failed rename keeps the dialog open and toasts", async () => {
    patch.mockImplementation((() =>
      Promise.resolve({
        data: undefined,
        error: { error: { code: "x", message: "Name taken." } },
        response: new Response(null, { status: 422 }),
      })) as never);
    const { user } = mount();
    const box = await openRename(user);
    await user.type(box, " v2");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(toasts.mock.calls.map((c: unknown[]) => (c[0] as { title: string }).title)).toEqual([
        "Name taken.",
      ]),
    );
    expect(screen.getByRole("textbox", { name: "Task name" })).toBeTruthy();
  });
});

describe("TaskMenu delete", () => {
  async function openDelete(user: ReturnType<typeof userEvent.setup>) {
    await openMenu(user);
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));
  }

  it("confirms with the label, deletes through the window and leaves for the list", async () => {
    const { user, router } = mount();
    await openDelete(user);
    expect(await screen.findByText("Delete this task?")).toBeTruthy();
    expect(screen.getByText(/Quarterly plan/)).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Delete task" }));
    await waitFor(() => expect(win.remove).toHaveBeenCalledWith("t1"));
    await waitFor(() => expect(router.state.location.pathname).toBe("/tasks"));
  });

  it("Cancel closes the confirmation and deletes nothing", async () => {
    const { user, router } = mount();
    await openDelete(user);
    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByText("Delete this task?")).toBeNull());
    expect(win.remove).not.toHaveBeenCalled();
    expect(router.state.location.pathname).toBe("/task");
  });

  it("a refused delete stays on the page with the dialog open", async () => {
    win.remove.mockResolvedValue(false);
    const { user, router } = mount();
    await openDelete(user);
    await user.click(await screen.findByRole("button", { name: "Delete task" }));
    await waitFor(() => expect(win.remove).toHaveBeenCalled());
    expect(router.state.location.pathname).toBe("/task");
    expect(screen.getByText("Delete this task?")).toBeTruthy();
  });
});
