/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { PASSWORD } from "./support/accounts";
import { stamp } from "./support/api";
import { mailAvailable, waitForMail } from "./support/mailbox";
import { accountMenu, composer, expect, knownBug, test } from "./fixtures";

// Signup from the form: the person gets an account, their own workspace and a session at once, and
// the address is proved later by the emailed link. Every flow signs up an address of its own.
test.describe("signup", () => {
  test("creates an account from the form and emails a verification link", async ({ page }) => {
    const email = `e2e-signup-${stamp()}@example.com`;
    await page.goto("/signup");
    await expect(page.getByRole("heading", { name: "Create your account" })).toBeVisible();
    await page.getByRole("textbox", { name: "Name" }).fill("Sam Signup");
    await page.getByRole("textbox", { name: "Email" }).fill(email);
    await page.getByRole("textbox", { name: "Password" }).fill(PASSWORD);
    await expect(page.getByRole("textbox", { name: "Invite code (optional)" })).toHaveValue("");
    await page.getByRole("button", { name: "Create account" }).click();

    await expect(composer(page)).toBeVisible();
    await expect(accountMenu(page, email)).toContainText("Sam Signup");
    // A new person starts in a workspace of their own.
    await expect(page.getByRole("button", { name: "Switch workspace" })).toContainText(
      "Sam Signup's workspace",
    );
    // The email is read from the stack's log; a stack whose log this run cannot read still proves
    // the form above.
    if (mailAvailable()) {
      const mail = await waitForMail(email, /Verify your email/);
      expect(mail.body).toContain("/verify-email#ev_");
    }
  });

  test("a short password is refused before anything is sent", async ({ page }) => {
    await page.goto("/signup");
    await page.getByRole("textbox", { name: "Name" }).fill("Sam Signup");
    await page.getByRole("textbox", { name: "Email" }).fill(`e2e-signup-${stamp()}@example.com`);
    await page.getByRole("textbox", { name: "Password" }).fill("too short");
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page.getByText("Use at least 12 characters.")).toBeVisible();
    await expect(page).toHaveURL(/\/signup$/);
  });

  test("an address that already has an account is refused in words", async ({ page, account }) => {
    // The API refuses an existing address with a deliberately vague 403 (it never confirms which
    // addresses exist), so the form owes the person its own written sentence, not the server's.
    knownBug(
      "web/app/routes/signup/route.tsx:57 maps only a 409 to written copy, but the API refuses an " +
        "existing address with a 403 (backend/prismoid/operations/auth.py:168), so the form shows " +
        'the raw lowercase "that email cannot be registered" and the 409 branch is dead code',
    );
    await page.goto("/signup");
    await page.getByRole("textbox", { name: "Name" }).fill("Sam Signup");
    await page.getByRole("textbox", { name: "Email" }).fill(account.email);
    await page.getByRole("textbox", { name: "Password" }).fill(PASSWORD);
    const refused = page.waitForResponse((r) => r.url().endsWith("/v1/auth/signup"));
    await page.getByRole("button", { name: "Create account" }).click();
    expect((await refused).status()).toBe(403);
    // The refusal keeps the person on the form, signed out, with a sentence of the form's own.
    await expect(page).toHaveURL(/\/signup$/);
    await expect(page.getByRole("button", { name: "Create account" })).toBeEnabled();
    await expect(page.getByText("that email cannot be registered", { exact: true })).toHaveCount(0);
  });
});
