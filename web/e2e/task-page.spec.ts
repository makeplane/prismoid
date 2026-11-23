/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { Page } from "@playwright/test";
import { escapeRegExp, expect, knownBug, test, toast } from "./fixtures";
import {
  ApiError,
  call,
  createHeldTask,
  createProject,
  createSettledTask,
  stamp,
} from "./support/api";

// The task page on tasks that never run: a held task (it waits for a first direction) and a settled
// one (held, then cancelled). Typing in the page's composer bar would launch the run, so these
// flows only use the header's verbs; Retry sits beside the feedback thumbs and is never pressed.

/** The header's own menu: the sidebar row of the same task has "Task actions for <title>". */
async function taskMenu(page: Page) {
  await page.getByRole("button", { name: "Task actions", exact: true }).click();
  return page.getByRole("menu", { name: "Task actions" });
}

function breadcrumb(page: Page) {
  return page.getByRole("navigation", { name: "Breadcrumb" });
}

async function statusOf(id: string, token: string): Promise<number> {
  return call(`/tasks/${id}`, { token }).then(
    () => 200,
    (e: ApiError) => e.status,
  );
}

test.describe("task page", () => {
  test("renames a held task and keeps the name", async ({ accountPage: page, account }) => {
    const task = await createHeldTask(account.token, "Draft the quarterly plan");
    const name = `Plan ${stamp()}`;
    await page.goto(`/tasks/${task.id}`);
    await (await taskMenu(page)).getByRole("menuitem", { name: "Rename" }).click();

    const dialog = page.getByRole("dialog", { name: "Rename task" });
    const field = dialog.getByRole("textbox", { name: "Task name" });
    await expect(field).toHaveValue(task.title);
    await field.fill(name);
    await dialog.getByRole("button", { name: "Save" }).click();
    await expect(dialog).toBeHidden();
    await expect(breadcrumb(page)).toContainText(name);

    await page.reload();
    await expect(breadcrumb(page)).toContainText(name);
    // The label changes; the ask on the thread stays as written.
    await expect(page.getByRole("region", { name: "Messages" })).toContainText(task.goal);
  });

  test("cancelling a held task settles it", async ({ accountPage: page, account }) => {
    const task = await createHeldTask(account.token, "Draft the quarterly plan");
    await page.goto(`/tasks/${task.id}`);
    const main = page.getByRole("main");
    await expect(main.getByRole("img", { name: "Status: queued" })).toBeVisible();
    let menu = await taskMenu(page);
    await expect(menu.getByRole("menuitem", { name: "Archive" })).toBeDisabled();
    await expect(menu.getByRole("menuitem", { name: "Delete" })).toBeDisabled();
    await menu.getByRole("menuitem", { name: "Cancel task" }).click();

    await expect(main.getByRole("img", { name: "Status: cancelled" })).toBeVisible();
    // Settled: the bar now offers a follow-up, and the answer carries its response actions.
    await expect(main.getByRole("textbox", { name: "Ask a follow-up…" })).toBeVisible();
    const actions = main.getByRole("group", { name: "Response actions" });
    for (const name of ["Copy", "Good response", "Bad response"])
      await expect(actions.getByRole("button", { name, exact: true })).toBeVisible();

    menu = await taskMenu(page);
    await expect(menu.getByRole("menuitem", { name: "Cancel task" })).toHaveCount(0);
    await expect(menu.getByRole("menuitem", { name: "Archive" })).toBeEnabled();
    await expect(menu.getByRole("menuitem", { name: "Delete" })).toBeEnabled();
    await page.keyboard.press("Escape");

    await page.reload();
    await expect(main.getByRole("img", { name: "Status: cancelled" })).toBeVisible();
  });

  test("a thumbs-up on a settled answer is kept", async ({ accountPage: page, account }) => {
    const task = await createSettledTask(account.token, "Compare the vendors");
    await page.goto(`/tasks/${task.id}`);
    const actions = page.getByRole("group", { name: "Response actions" });
    const good = actions.getByRole("button", { name: "Good response" });
    const bad = actions.getByRole("button", { name: "Bad response" });
    await expect(good).toHaveAttribute("aria-pressed", "false");
    // The thumb presses at once, before the server has it: wait for the save before reloading,
    // or the reload cancels it in flight.
    const saved = page.waitForResponse(
      (r) => r.url().endsWith(`/tasks/${task.id}/feedback`) && r.request().method() === "POST",
    );
    await good.click();
    await expect(good).toHaveAttribute("aria-pressed", "true");
    await expect(bad).toHaveAttribute("aria-pressed", "false");
    expect((await saved).ok()).toBe(true);

    await page.reload();
    await expect(good).toHaveAttribute("aria-pressed", "true");
  });

  test("deletes a settled task from its menu", async ({ accountPage: page, account }) => {
    const task = await createSettledTask(account.token, "Compare the vendors");
    await page.goto(`/tasks/${task.id}`);
    await (await taskMenu(page)).getByRole("menuitem", { name: "Delete" }).click();

    const confirm = page.getByRole("alertdialog", { name: "Delete this task?" });
    await expect(confirm).toContainText(task.title);
    await confirm.getByRole("button", { name: "Delete task" }).click();
    await expect(page).toHaveURL(/\/tasks$/);
    await expect(page.getByText("Nothing here yet")).toBeVisible();
    expect(await statusOf(task.id, account.token)).toBe(404);
  });

  test("archives a settled task from its menu", async ({ accountPage: page, account }) => {
    const task = await createSettledTask(account.token, "Compare the vendors");
    await page.goto(`/tasks/${task.id}`);
    await (await taskMenu(page)).getByRole("menuitem", { name: "Archive" }).click();
    await expect((await taskMenu(page)).getByRole("menuitem", { name: "Unarchive" })).toBeVisible();
    await page.keyboard.press("Escape");

    const listed = page
      .getByRole("main")
      .getByRole("link", { name: new RegExp(escapeRegExp(task.title)) });
    await page.goto("/tasks");
    await expect(page.getByText("Nothing here yet")).toBeVisible();
    await page.goto("/tasks?archived=1");
    await expect(page.getByRole("button", { name: "Remove filter Archived" })).toBeVisible();
    await expect(listed).toBeVisible();
  });

  test("forking makes a held branch and opens it", async ({ accountPage: page, account }) => {
    const task = await createHeldTask(account.token, "Compare the vendors");
    await page.goto(`/tasks/${task.id}`);
    await (await taskMenu(page)).getByRole("menuitem", { name: "Fork this task" }).click();

    await expect(page).not.toHaveURL(new RegExp(task.id));
    await expect(page).toHaveURL(/\/tasks\/[0-9a-f-]{36}$/);
    const childId = page.url().split("/").pop()!;
    await expect(breadcrumb(page)).toContainText(`[Forked] ${task.title}`);
    const thread = page.getByRole("region", { name: "Messages" });
    await expect(thread.getByRole("link", { name: task.goal })).toBeVisible();
    await expect(thread.getByRole("status").last()).toContainText(/Held\. .*has not started/);

    const child = await call<{ status: string; title: string }>(`/tasks/${childId}`, {
      token: account.token,
    });
    expect(child).toMatchObject({ status: "created", title: `[Forked] ${task.title}` });
  });

  test("sharing moves a personal task into a project", async ({ accountPage: page, account }) => {
    const project = await createProject(account.token, `Bench ${stamp()}`);
    const task = await createSettledTask(account.token, "Compare the vendors");
    await page.goto(`/tasks/${task.id}`);
    await page.getByRole("button", { name: "Share this task" }).click();
    const popover = page.getByRole("dialog").filter({ hasText: "Only you can see this" });
    await popover.getByRole("button", { name: `Move to ${project.name}` }).click();
    await page.getByRole("button", { name: `Move it to ${project.name}` }).click();

    await expect(toast(page, `Moved to ${project.name}`)).toBeVisible();
    const canonical = `/projects/${project.id}/tasks/${task.id}`;
    await expect(page).toHaveURL(new RegExp(`${canonical}$`));
    await expect(breadcrumb(page).getByRole("link", { name: project.name })).toBeVisible();

    // A project task's old personal address lands on its project address.
    await page.goto(`/tasks/${task.id}`);
    await expect(page).toHaveURL(new RegExp(`${canonical}$`));
    await expect(breadcrumb(page).getByRole("link", { name: project.name })).toBeVisible();
  });

  test("the plan and outputs rail toggles and stays toggled", async ({
    accountPage: page,
    account,
  }) => {
    const task = await createHeldTask(account.token, "Draft the quarterly plan");
    await page.goto(`/tasks/${task.id}`);
    const toggle = page.getByRole("button", { name: "Toggle plan and artifacts" });
    const rail = page.getByRole("main").getByRole("complementary");
    await expect(toggle).toHaveAttribute("aria-pressed", "true");
    await expect(rail.getByRole("button", { name: "Progress" })).toBeVisible();
    await expect(rail.getByText("No plan yet")).toBeVisible();
    await expect(rail.getByRole("button", { name: "Outputs" })).toBeVisible();
    await expect(rail.getByText("Nothing published yet")).toBeVisible();

    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-pressed", "false");
    await expect(rail).toHaveCount(0);
    await page.reload();
    await expect(toggle).toHaveAttribute("aria-pressed", "false");
    await expect(rail).toHaveCount(0);
    await toggle.click();
    await expect(rail.getByText("No plan yet")).toBeVisible();
  });

  test("a held task is not offered a pause it cannot take", async ({
    accountPage: page,
    account,
  }) => {
    knownBug(
      "web/app/routes/tasks.$id/task-menu.tsx:78 - the menu offers Pause on any live task, but " +
        "backend/prismoid/operations/steering.py:505 pauses only a running or waiting one, so a " +
        "held task's Pause answers 422 'cannot pause a task that is created'",
    );
    const task = await createHeldTask(account.token, "Draft the quarterly plan");
    await page.goto(`/tasks/${task.id}`);
    const menu = await taskMenu(page);
    await expect(menu.getByRole("menuitem", { name: "Cancel task" })).toBeVisible();
    await expect(menu.getByRole("menuitem", { name: "Pause" })).toHaveCount(0);
  });
});
