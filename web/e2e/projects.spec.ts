/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { expect, test } from "./fixtures";

test("creates a project and opens it from the list", async ({ signedInPage: page }) => {
  const name = `Field research ${Date.now().toString(36)}`;
  await page.goto("/projects");
  await page.getByRole("button", { name: "New project" }).click();

  const dialog = page.getByRole("dialog", { name: "New project" });
  const create = dialog.getByRole("button", { name: "Create project" });
  await expect(create).toBeDisabled();
  await dialog.getByRole("textbox", { name: "Name" }).fill(name);
  await create.click();

  // Creating opens the new project.
  await expect(page).toHaveURL(/\/projects\/[0-9a-f-]{36}$/);
  const projectUrl = page.url();
  await expect(page.getByText(name).first()).toBeVisible();

  // The list carries it, and opening it from there reaches the same project.
  await page.getByRole("link", { name: "Projects" }).first().click();
  await expect(page).toHaveURL(/\/projects$/);
  await page
    .getByRole("link", { name: new RegExp(name) })
    .first()
    .click();
  await expect(page).toHaveURL(projectUrl);
});
