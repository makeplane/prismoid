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

  // The page signs in a fresh account (not the shared one): the login door counts sign-ins per
  // account, and this run's other form sign-ins already spend the shared account's.
  test("a signed-out deep link comes back to its page after sign-in", async ({ page, account }) => {
    await page.goto("/projects");
    await expect(page).toHaveURL(/\/login\?next=%2Fprojects$/);
    await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();

    await page.getByRole("textbox", { name: "Email" }).fill(account.email);
    await page.getByRole("textbox", { name: "Password" }).fill(account.password);
    await page.getByRole("button", { name: "Sign in" }).click();

    await expect(page).toHaveURL(/\/projects$/);
    await expect(page.getByRole("navigation", { name: "Breadcrumb" })).toHaveText("Projects");
  });

  test("the form's account link opens signup and keeps the way back", async ({ page }) => {
    await page.goto("/projects");
    await expect(page).toHaveURL(/\/login\?next=%2Fprojects$/);
    await page.getByRole("link", { name: "Create an account" }).click();

    // Someone without an account yet still lands where the link was taking them once they have one.
    await expect(page).toHaveURL(/\/signup\?next=%2Fprojects$/);
    await expect(page.getByRole("heading", { name: "Create your account" })).toBeVisible();
    await page.getByRole("link", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/login\?next=%2Fprojects$/);
    await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();
  });
});
