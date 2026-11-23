/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { Page } from "@playwright/test";
import { createHeldTask } from "./support/api";
import { accountMenu, composer, escapeRegExp, expect, knownBug, test, toast } from "./fixtures";

// The app shell: the sidebar's nav, its collapse, the global chords, the account menu and the
// recent-task rows. Every flow runs on a fresh account, so its sidebar holds only what it made.

/** A recent-task row's link in the sidebar: its name is the status mark's label, then the title. */
function taskLink(page: Page, title: string) {
  return page.getByRole("link", { name: new RegExp(`${escapeRegExp(title)}$`) });
}

test.describe("app shell", () => {
  test("every sidebar nav link reaches its page", async ({ accountPage: page }) => {
    await page.goto("/allowed-sites");
    const crumbs = page.getByRole("navigation", { name: "Breadcrumb" });
    for (const name of [
      "Inbox",
      "Tasks",
      "Projects",
      "Routines",
      "Bots",
      "Connectors",
      "Allowed sites",
    ]) {
      await page.getByRole("link", { name, exact: true }).click();
      await expect(crumbs).toHaveText(name);
    }
    // Home is the landing: no trail, the composer.
    await page.getByRole("link", { name: "Home", exact: true }).click();
    await expect(page).toHaveURL(/\/$/);
    await expect(composer(page)).toBeVisible();
    await expect(crumbs).toBeHidden();
  });

  test("the sidebar collapses to its rail and back, by button and by chord", async ({
    accountPage: page,
  }) => {
    await page.goto("/tasks");
    const switcher = page.getByRole("button", { name: "Switch workspace" });
    const expand = page.getByRole("button", { name: "Expand sidebar" });
    await expect(switcher).toBeVisible();

    await page.getByRole("button", { name: "Toggle Sidebar", exact: true }).click();
    await expect(expand).toBeVisible();
    await expect(switcher).toBeHidden();
    await expand.click();
    await expect(switcher).toBeVisible();
    await expect(expand).toBeHidden();

    await page.keyboard.press("ControlOrMeta+b");
    await expect(expand).toBeVisible();
    await page.keyboard.press("ControlOrMeta+b");
    await expect(switcher).toBeVisible();
  });

  test("the new-task chord goes home from any page", async ({ accountPage: page }) => {
    await page.goto("/routines");
    await expect(page.getByRole("navigation", { name: "Breadcrumb" })).toHaveText("Routines");
    await page.keyboard.press("ControlOrMeta+Shift+O");
    await expect(page).toHaveURL(/\/$/);
    await expect(composer(page)).toBeVisible();
  });

  test("the account menu's Usage opens Settings on the usage pane", async ({
    accountPage: page,
    account,
  }) => {
    await page.goto("/");
    await accountMenu(page, account.email).click();
    await page.getByRole("menuitem", { name: "Usage" }).click();
    const settings = page.getByRole("dialog", { name: "Settings" });
    await expect(settings.getByRole("heading", { name: "Usage" })).toBeVisible();
    await expect(settings.getByRole("button", { name: "Usage" })).toHaveAttribute(
      "aria-current",
      "true",
    );
    await expect(settings.getByText("No projects yet")).toBeVisible();
  });

  test("renames a recent task in place and keeps the name", async ({
    accountPage: page,
    account,
  }) => {
    const task = await createHeldTask(account.token, "Summarize the tide pool field notes");
    await page.goto("/allowed-sites");
    await expect(taskLink(page, task.title)).toBeVisible();

    await page.getByRole("button", { name: `Task actions for ${task.title}` }).click();
    await page.getByRole("menuitem", { name: "Rename" }).click();
    const field = page.getByRole("textbox", { name: "Task name" });
    await expect(field).toHaveValue(task.title);
    await field.fill("Tide pool notes, renamed");
    await field.press("Enter");
    await expect(taskLink(page, "Tide pool notes, renamed")).toBeVisible();

    await page.reload();
    await expect(taskLink(page, "Tide pool notes, renamed")).toBeVisible();
    await expect(taskLink(page, task.title)).toBeHidden();
  });

  test("pins a recent task above the rest and unpins it", async ({
    accountPage: page,
    account,
  }) => {
    const task = await createHeldTask(account.token, "Summarize the tide pool field notes");
    await page.goto("/allowed-sites");
    const pinned = page.getByText("Pinned", { exact: true });
    await expect(pinned).toBeHidden();

    await page.getByRole("button", { name: `Task actions for ${task.title}` }).click();
    await page.getByRole("menuitem", { name: "Pin" }).click();
    await expect(pinned).toBeVisible();
    // The only task moved, so the recent group is gone with it.
    await expect(page.getByText("Recent tasks", { exact: true })).toBeHidden();

    await page.reload();
    await expect(pinned).toBeVisible();
    await page.getByRole("button", { name: `Task actions for ${task.title}` }).click();
    await page.getByRole("menuitem", { name: "Unpin" }).click();
    await expect(pinned).toBeHidden();
    await expect(page.getByText("Recent tasks", { exact: true })).toBeVisible();

    await page.reload();
    await expect(pinned).toBeHidden();
    await expect(taskLink(page, task.title)).toBeVisible();
  });

  test("a held task the server will not delete stays listed", async ({
    accountPage: page,
    account,
  }) => {
    knownBug(
      "web/app/components/tasks/task-window.tsx:206 drops the row before the DELETE answers and " +
        "never puts it back when the server refuses (409 for a task that is not settled), so the " +
        "sidebar hides a task that still exists until the next navigation",
    );
    const task = await createHeldTask(account.token, "Summarize the tide pool field notes");
    await page.goto("/allowed-sites");
    await page.getByRole("button", { name: `Task actions for ${task.title}` }).click();
    await page.getByRole("menuitem", { name: "Delete" }).click();
    const confirm = page.getByRole("alertdialog", { name: "Delete this task?" });
    await confirm.getByRole("button", { name: "Delete task" }).click();

    // The refusal is said out loud...
    await expect(toast(page, "cancel the task before deleting it")).toBeVisible();
    await expect(confirm).toBeHidden();
    // ...and the task, which still exists, is still listed.
    await expect(taskLink(page, task.title)).toBeVisible();
  });
});
