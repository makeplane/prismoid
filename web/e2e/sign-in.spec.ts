/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { composer, expect, test } from "./fixtures";

test.describe("sign-in", () => {
  test("a wrong password keeps the person on the form", async ({ page, seed }) => {
    await page.goto("/login");
    await page.getByRole("textbox", { name: "Email" }).fill(seed.email);
    await page.getByRole("textbox", { name: "Password" }).fill("not the password");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByText("Invalid email or password.")).toBeVisible();
    await expect(page).toHaveURL(/\/login/);
  });

  test("signs in with email and password, then signs out", async ({ page, seed }) => {
    await page.goto("/");
    // A signed-out visit to the app lands on the form.
    await expect(page).toHaveURL(/\/login/);
    await page.getByRole("textbox", { name: "Email" }).fill(seed.email);
    await page.getByRole("textbox", { name: "Password" }).fill(seed.password);
    await page.getByRole("textbox", { name: "Password" }).press("Enter");

    await expect(composer(page)).toBeVisible();
    await expect(page.getByRole("button", { name: new RegExp(seed.email) })).toBeVisible();

    await page.getByRole("button", { name: new RegExp(seed.email) }).click();
    await page.getByRole("menuitem", { name: "Log out" }).click();
    await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();

    // The session is gone, not only hidden: the app sends a fresh visit back to the form.
    await page.goto("/tasks");
    await expect(page).toHaveURL(/\/login/);
  });
});
