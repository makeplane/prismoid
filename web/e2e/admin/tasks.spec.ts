/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { signUp } from "../support/accounts";
import { call, createHeldTask, stamp } from "../support/api";
import { escapeRegExp, expect, test, toast } from "../fixtures";

// The task console: every task in the system, searchable and sliced by status, with a peek that
// reads the durable record and an operator's one hard verb, cancel. The tasks here are held (a
// routine fired with posture "hold"), so nothing ever plans or runs and no model is called.

const SEARCH = "Search title, goal, or task ID…";

async function heldTaskOfNewPerson() {
  const owner = await signUp("e2e-admin-tasks");
  const task = await createHeldTask(owner.token, `Reconcile the moth survey ${stamp()}`);
  return { owner, task };
}

test.describe("console tasks", () => {
  test("search finds a fresh held task, and the status chips slice the list", async ({
    adminPage: page,
  }) => {
    const { owner, task } = await heldTaskOfNewPerson();
    await page.goto("/tasks");
    const main = page.getByRole("main");
    const search = page.getByRole("textbox", { name: SEARCH });
    // Every task row names its turn; once a search has reached the server (the box debounces,
    // and the search rides the URL, so a reload runs it fresh) this task's row is the only one.
    const rows = main.getByRole("listitem").filter({ hasText: /· turn \d+/ });
    const row = rows.filter({ hasText: task.title });
    await search.fill(task.goal);
    await expect(page).toHaveURL(/q=/);
    await page.reload();
    await expect(search).toHaveValue(task.goal);
    await expect(row).toHaveCount(1);
    await expect(rows).toHaveCount(1);
    await expect(row).toContainText("· held");
    await expect(row.getByRole("img", { name: "Status: queued" })).toBeVisible();
    await expect(row.getByRole("link", { name: owner.email })).toBeVisible();

    // The id finds it as well.
    await search.fill(task.id);
    await expect(page).toHaveURL(new RegExp(`q=${task.id}`));
    await page.reload();
    await expect(search).toHaveValue(task.id);
    await expect(row).toHaveCount(1);
    await expect(rows).toHaveCount(1);

    // A held task has not started: the queued chip keeps it, a delivered-only slice drops it.
    await main.getByRole("button", { name: "queued", exact: true }).click();
    await expect(page).toHaveURL(/status=created/);
    await expect(row).toHaveCount(1);
    await main.getByRole("button", { name: "queued", exact: true }).click();
    await main.getByRole("button", { name: "delivered", exact: true }).click();
    await expect(page).toHaveURL(/status=delivered/);
    await expect(main.getByText(`No matches for “${task.id}”`)).toBeVisible();
    await main.getByRole("button", { name: "delivered", exact: true }).click();
    await expect(row).toHaveCount(1);

    // The owner's email leads to their page.
    await row.getByRole("link", { name: owner.email }).click();
    await expect(page).toHaveURL(new RegExp(`/users/${owner.id}$`));
  });

  test("the peek reads the task's status and record", async ({ adminPage: page }) => {
    const { owner, task } = await heldTaskOfNewPerson();
    await page.goto(`/tasks?q=${task.id}`);
    await page
      .getByRole("main")
      .getByRole("button", { name: new RegExp(escapeRegExp(task.title)) })
      .click();
    await expect(page).toHaveURL(new RegExp(`task=${task.id}`));

    const peek = page.getByRole("dialog", { name: "Task" });
    await expect(peek.getByText(task.title)).toBeVisible();
    await expect(peek.getByRole("link", { name: owner.email })).toBeVisible();
    await expect(peek.getByText("queued", { exact: true })).toBeVisible();
    // The record's first frame is what was asked.
    await expect(peek.getByText(task.goal)).toBeVisible();
    await expect(peek.getByText(/live - re-reading every \d+s/)).toBeVisible();

    // The peek is in the URL: a reload opens it again.
    await page.reload();
    await expect(peek.getByText(task.goal)).toBeVisible();
    await peek.getByRole("button", { name: "Close" }).click();
    await expect(peek).toBeHidden();
    await expect(page).not.toHaveURL(/task=/);
  });

  test("an operator cancels a held task with a reason", async ({ adminPage: page }) => {
    const { owner, task } = await heldTaskOfNewPerson();
    await page.goto(`/tasks?task=${task.id}`);
    const peek = page.getByRole("dialog", { name: "Task" });
    await expect(peek.getByText("queued", { exact: true })).toBeVisible();
    await peek.getByRole("button", { name: "Cancel task" }).click();

    const dialog = page.getByRole("dialog", { name: "Cancel this task?" });
    const confirm = dialog.getByRole("button", { name: "Cancel task" });
    await expect(confirm).toBeDisabled();
    const reason = `Runaway spend on a shared key ${stamp()}`;
    await dialog.getByRole("textbox", { name: "Reason" }).fill(reason);
    await confirm.click();
    await expect(toast(page, "Cancelled. Status is now cancelled.")).toBeVisible();
    await expect(dialog).toBeHidden();
    await expect(peek.getByText("cancelled", { exact: true })).toBeVisible();
    // A settled task has nothing left to cancel.
    await expect(peek.getByRole("button", { name: "Cancel task" })).toHaveCount(0);

    // The owner's own view agrees.
    const seen = await call<{ status: string }>(`/tasks/${task.id}`, { token: owner.token });
    expect(seen.status).toBe("cancelled");

    // The act and its reason are on the audit trail.
    await page.goto("/audit");
    const entry = page
      .getByRole("main")
      .getByRole("listitem")
      .filter({ hasText: "task.cancel" })
      .filter({ has: page.getByRole("link", { name: `task:${task.id}` }) });
    await expect(entry).toContainText(reason);
  });
});
