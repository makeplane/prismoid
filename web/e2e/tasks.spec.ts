/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { Page } from "@playwright/test";
import { escapeRegExp, expect, test, toast } from "./fixtures";
import {
  ApiError,
  call,
  createHeldTask,
  createProject,
  createSettledTask,
  stamp,
} from "./support/api";

// The task list. Every flow signs up its own account, so what the list shows is exactly the tasks
// the flow made: held ones (never planned or run) and settled ones (held, then cancelled), which are
// the only tasks archive and delete accept.

/** A task's row in the list, by its title. */
function row(page: Page, title: string) {
  return page.getByRole("main").getByRole("link", { name: new RegExp(escapeRegExp(title)) });
}

/** One status group of the list: its sections carry no name, so they are found by their heading. */
function group(page: Page, label: string) {
  return page
    .getByRole("main")
    .locator("section")
    .filter({ has: page.getByRole("heading", { name: label, exact: true }) });
}

/** The total a status group's header states beside its name. */
function groupCount(page: Page, label: string) {
  return group(page, label).getByText(/^\d+$/);
}

/** Tick a row's checkbox: it shows only while the row is hovered, and the row's own hit area takes
 *  the click (the box itself ignores the pointer), so the click is forced onto the hovered box. */
async function select(page: Page, title: string) {
  await row(page, title).hover();
  await page
    .getByRole("main")
    .getByRole("checkbox", { name: `Select ${title}` })
    .click({ force: true });
}

async function filter(page: Page, item: string) {
  await page.getByRole("button", { name: "Filter", exact: true }).click();
  await page
    .getByRole("menu", { name: "Filter" })
    .getByRole("menuitemradio", { name: item })
    .click();
  await page.keyboard.press("Escape");
}

test.describe("task list", () => {
  test("a fresh account has nothing listed", async ({ accountPage: page }) => {
    await page.goto("/tasks");
    await expect(page.getByText("Nothing here yet")).toBeVisible();
    await expect(page.getByText("Start something new.")).toBeVisible();
  });

  test("tasks sit in their status groups and search narrows them", async ({
    accountPage: page,
    account,
  }) => {
    const held = await createHeldTask(account.token, "Draft the quarterly plan");
    const settled = await createSettledTask(account.token, "Compare the vendors");
    await page.goto("/tasks");

    // A held task has not started: the list files it under In progress and calls it queued.
    await expect(group(page, "In progress").getByRole("link")).toHaveCount(1);
    await expect(group(page, "In progress").getByRole("link")).toContainText(held.title);
    await expect(group(page, "In progress").getByRole("link")).toContainText("queued");
    await expect(groupCount(page, "In progress")).toHaveText("1");
    await expect(group(page, "Failed & cancelled").getByRole("link")).toContainText(settled.title);
    await expect(group(page, "Failed & cancelled").getByRole("link")).toContainText("cancelled");

    const search = page.getByRole("textbox", { name: "Search tasks…" });
    // The routine name is the start of each title and carries its own stamp.
    await search.fill(held.title.split(" · ")[0]);
    await expect(row(page, held.title)).toBeVisible();
    await expect(row(page, settled.title)).toHaveCount(0);
    await search.fill(`nothing-${stamp()}`);
    await expect(page.getByText(/^No matches for “nothing-/)).toBeVisible();
    await search.fill("");
    await expect(row(page, settled.title)).toBeVisible();
    await expect(row(page, held.title)).toBeVisible();
  });

  test("the Filter menu narrows the list and its chips undo it", async ({
    accountPage: page,
    account,
  }) => {
    const project = await createProject(account.token, `Bench ${stamp()}`);
    const held = await createHeldTask(account.token, "Draft the quarterly plan");
    const settled = await createSettledTask(account.token, "Compare the vendors");
    await call(`/tasks/${settled.id}/move`, {
      token: account.token,
      body: { project_id: project.id },
    });
    await page.goto("/tasks");
    await expect(row(page, held.title)).toBeVisible();

    await filter(page, "Failed & cancelled");
    const statusChip = page.getByRole("button", {
      name: "Remove filter Status: Failed & cancelled",
    });
    await expect(statusChip).toBeVisible();
    await expect(row(page, held.title)).toHaveCount(0);
    await expect(row(page, settled.title)).toBeVisible();
    await statusChip.click();
    await expect(row(page, held.title)).toBeVisible();

    await filter(page, "Fleet");
    await expect(page.getByRole("button", { name: "Remove filter Mode: fleet" })).toBeVisible();
    await expect(page.getByText("Nothing here yet")).toBeVisible();
    await page.getByRole("button", { name: "Remove filter Mode: fleet" }).click();
    await expect(row(page, held.title)).toBeVisible();

    await filter(page, project.name);
    await expect(
      page.getByRole("button", { name: `Remove filter Project: ${project.name}` }),
    ).toBeVisible();
    await expect(row(page, settled.title)).toBeVisible();
    await expect(row(page, held.title)).toHaveCount(0);

    await page.getByRole("button", { name: "Filter", exact: true }).click();
    await page.getByRole("menuitem", { name: "Clear filters" }).click();
    await expect(page.getByRole("button", { name: /^Remove filter/ })).toHaveCount(0);
    await expect(row(page, held.title)).toBeVisible();
    await expect(row(page, settled.title)).toBeVisible();
  });

  test("Display options lay the list out as a board and regroup it", async ({
    accountPage: page,
    account,
  }) => {
    const held = await createHeldTask(account.token, "Draft the quarterly plan");
    const settled = await createSettledTask(account.token, "Compare the vendors");
    await page.goto("/tasks");
    await expect(row(page, held.title)).toBeVisible();

    const display = async (item: string) => {
      await page.getByRole("button", { name: "Display options" }).click();
      await page
        .getByRole("menu", { name: "Display options" })
        .getByRole("menuitemradio", { name: item, exact: true })
        .click();
      await page.keyboard.press("Escape");
    };
    const main = page.getByRole("main");
    await display("Board");
    await expect(main.getByRole("region", { name: "In progress" })).toContainText(held.title);
    await expect(main.getByRole("region", { name: "Failed & cancelled" })).toContainText(
      settled.title,
    );
    await expect(main.getByRole("region", { name: "Delivered" })).toBeVisible();

    await display("Mode");
    await expect(main.getByRole("region", { name: "standard" })).toContainText(held.title);
    await expect(main.getByRole("region", { name: "standard" })).toContainText(settled.title);

    // One flat list: no group headings at all.
    await display("List");
    await display("None");
    await expect(main.getByRole("heading", { level: 2 })).toHaveCount(0);
    await expect(row(page, held.title)).toBeVisible();
    await expect(row(page, settled.title)).toBeVisible();
  });

  test("bulk archive files a settled task away and unarchive brings it back", async ({
    accountPage: page,
    account,
  }) => {
    const settled = await createSettledTask(account.token, "Compare the vendors");
    await page.goto("/tasks");
    await select(page, settled.title);
    const main = page.getByRole("main");
    await expect(main.getByText("1 selected")).toBeVisible();
    await main.getByRole("button", { name: "Archive", exact: true }).click();
    await expect(toast(page, "Archived 1 task.")).toBeVisible();
    await expect(row(page, settled.title)).toHaveCount(0);
    await expect(page.getByText("Nothing here yet")).toBeVisible();

    await filter(page, "Archived");
    const archivedChip = page.getByRole("button", { name: "Remove filter Archived" });
    await expect(archivedChip).toBeVisible();
    await select(page, settled.title);
    await main.getByRole("button", { name: "Unarchive", exact: true }).click();
    await expect(toast(page, "Unarchived 1 task.")).toBeVisible();
    await expect(row(page, settled.title)).toHaveCount(0);

    await archivedChip.click();
    await expect(row(page, settled.title)).toBeVisible();
    await page.reload();
    await expect(row(page, settled.title)).toBeVisible();
  });

  test("bulk delete removes a settled task for good", async ({ accountPage: page, account }) => {
    const gone = await createSettledTask(account.token, "Compare the vendors");
    const kept = await createSettledTask(account.token, "Price the hardware");
    await page.goto("/tasks");
    await select(page, gone.title);
    await page.getByRole("main").getByRole("button", { name: "Delete", exact: true }).click();

    const confirm = page.getByRole("alertdialog", { name: "Delete 1 task?" });
    await confirm.getByRole("button", { name: "Delete", exact: true }).click();
    await expect(toast(page, "Deleted 1 task.")).toBeVisible();
    await expect(row(page, gone.title)).toHaveCount(0);
    await page.reload();
    await expect(row(page, kept.title)).toBeVisible();
    await expect(row(page, gone.title)).toHaveCount(0);
    const status = await call(`/tasks/${gone.id}`, { token: account.token }).then(
      () => 200,
      (e: ApiError) => e.status,
    );
    expect(status).toBe(404);
  });

  test("the archived view counts the archive, not the active list", async ({
    accountPage: page,
    account,
  }) => {
    const tasks = [];
    for (const goal of ["Compare the vendors", "Price the hardware", "Book the venue"])
      tasks.push(await createSettledTask(account.token, goal));
    await call(`/tasks/${tasks[0].id}/archive`, { token: account.token, body: {} });
    await page.goto("/tasks");
    await expect(group(page, "Failed & cancelled").getByRole("link")).toHaveCount(2);
    await expect(groupCount(page, "Failed & cancelled")).toHaveText("2");

    await filter(page, "Archived");
    await expect(group(page, "Failed & cancelled").getByRole("link")).toHaveCount(1);
    await expect(row(page, tasks[0].title)).toBeVisible();
    await expect(groupCount(page, "Failed & cancelled")).toHaveText("1");
  });
});
