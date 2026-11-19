/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { expect, test } from "./fixtures";

// The seeded task is held: a real record with its goal on the thread, never planned or run.
test("opens a task record from the task list", async ({ signedInPage: page, seed }) => {
  await page.goto("/tasks");
  await page
    .getByRole("main")
    .getByRole("link", { name: new RegExp(seed.task.title) })
    .first()
    .click();
  await expect(page).toHaveURL(new RegExp(`/tasks/${seed.task.id}$`));

  await expect(page.getByRole("navigation", { name: "Breadcrumb" })).toContainText(seed.task.title);
  const thread = page.getByRole("region", { name: "Messages" });
  await expect(thread).toContainText(seed.task.goal);
  // Held: it waits for a direction, and nothing has been planned or published.
  await expect(thread.getByRole("status").first()).toContainText(/Held\. .*has not started/);
  await expect(page.getByText("No plan yet")).toBeVisible();
  await expect(page.getByText("Nothing published yet")).toBeVisible();
});

test("a task id nobody can see is not found", async ({ signedInPage: page }) => {
  await page.goto("/tasks/00000000-0000-4000-8000-000000000000");
  await expect(page.getByText("Not found", { exact: true })).toBeVisible();
});
