/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { expect, test } from "./fixtures";

test("edits the profile name in settings and keeps it", async ({ signedInPage: page, seed }) => {
  await page.goto("/");
  const openSettings = async () => {
    await page.getByRole("button", { name: new RegExp(seed.email) }).click();
    await page.getByRole("menuitem", { name: "Settings" }).click();
    return page.getByRole("dialog", { name: "Settings" });
  };

  let settings = await openSettings();
  const first = settings.getByRole("textbox", { name: "First name" });
  const save = settings.getByRole("button", { name: "Save" });
  await expect(first).toHaveValue("Ada");
  await expect(save).toBeDisabled();
  await first.fill("Grace");
  await expect(save).toBeEnabled();
  await save.click();
  await expect(save).toBeDisabled();

  // The change is the server's now: a reload reads it back.
  await page.reload();
  settings = await openSettings();
  await expect(settings.getByRole("textbox", { name: "First name" })).toHaveValue("Grace");
});

test("switches the appearance to dark", async ({ signedInPage: page, seed }) => {
  await page.goto("/");
  await page.getByRole("button", { name: new RegExp(seed.email) }).click();
  await page.getByRole("menuitem", { name: "Settings" }).click();
  const settings = page.getByRole("dialog", { name: "Settings" });
  await settings.getByRole("button", { name: "Dark" }).click();
  await expect(settings.getByRole("button", { name: "Dark" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.keyboard.press("Escape");
  await expect(settings).toBeHidden();
});
