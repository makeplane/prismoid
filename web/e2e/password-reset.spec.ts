/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { signIn } from "./support/accounts";
import { call } from "./support/api";
import { codeIn, mailAvailable, waitForMail } from "./support/mailbox";
import { composer, expect, test } from "./fixtures";

// A forgotten password (ADR-031): the emailed single-use code is the whole authentication, and
// completing a reset revokes every live session. Each flow resets its own account.
test.describe("password reset", () => {
  test.skip(!mailAvailable(), "needs the stack's mail log (E2E_API_LOG)");

  test("resets the password with the emailed code, then signs in with it", async ({
    page,
    account,
  }) => {
    // A session from before the reset, to prove the reset ends it.
    const before = await signIn(account);

    await page.goto("/login");
    await page.getByRole("link", { name: "Forgot your password?" }).click();
    await expect(page.getByRole("heading", { name: "Forgot your password?" })).toBeVisible();
    await page.getByRole("textbox", { name: "Email" }).fill(account.email);
    await page.getByRole("button", { name: "Send reset code" }).click();
    await expect(page.getByText("Check your email")).toBeVisible();

    const mail = await waitForMail(account.email, /password reset/);
    await page.getByRole("button", { name: "I have my code" }).click();
    await expect(page.getByRole("heading", { name: "Reset your password" })).toBeVisible();
    await page.getByRole("textbox", { name: "Reset code" }).fill(codeIn(mail, "cr_"));
    const fresh = "a much longer passphrase now";
    await page.getByRole("textbox", { name: "New password" }).fill(fresh);
    await page.getByRole("button", { name: "Reset password" }).click();
    await expect(page.getByText("Password updated")).toBeVisible();

    // Every session from before is gone.
    await expect(call("/auth/me", { token: before.token })).rejects.toMatchObject({ status: 401 });

    await expect(page.getByText(/Every signed-in session was signed out/)).toBeVisible();
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.getByRole("textbox", { name: "Email" }).fill(account.email);
    await page.getByRole("textbox", { name: "Password" }).fill(account.password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByText("Invalid email or password.")).toBeVisible();
    await page.getByRole("textbox", { name: "Password" }).fill(fresh);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(composer(page)).toBeVisible();
  });

  test("a code that was never issued is refused", async ({ page }) => {
    await page.goto("/reset-password");
    await page
      .getByRole("textbox", { name: "Reset code" })
      .fill("cr_thisCodeWasNeverIssuedByTheServer");
    await page.getByRole("textbox", { name: "New password" }).fill("a much longer passphrase now");
    await page.getByRole("button", { name: "Reset password" }).click();
    await expect(
      page.getByText("That code is invalid or has expired. Request a new one."),
    ).toBeVisible();
  });

  test("the reset link prefills the code", async ({ page }) => {
    await page.goto("/reset-password?code=cr_prefilledFromTheLink");
    await expect(page.getByRole("textbox", { name: "Reset code" })).toHaveValue(
      "cr_prefilledFromTheLink",
    );
  });
});
