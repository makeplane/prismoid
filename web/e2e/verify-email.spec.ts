/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { call } from "./support/api";
import { codeIn, mailAvailable, mailCount, waitForMail } from "./support/mailbox";
import { composer, expect, test } from "./fixtures";

type Me = { email_verified: boolean };

// The emailed verification link (ADR-031): its single-use ev_ code rides the URL fragment, is
// posted once by a signed-in page, and waits through sign-in for a signed-out one. Each flow proves
// the address of its own fresh account.
test.describe("verify email", () => {
  test("the emailed link proves the address of the signed-in person", async ({
    accountPage: page,
    account,
  }) => {
    test.skip(!mailAvailable(), "needs the stack's mail log (E2E_API_LOG)");
    const code = codeIn(await waitForMail(account.email, /Verify your email/), "ev_");
    await page.goto(`/verify-email#${code}`);
    await expect(page.getByRole("heading", { name: "Email verified" })).toBeVisible();
    await expect(page.getByText("Your email address is verified.")).toBeVisible();
    // The code leaves the address bar once read, so it never lands in history.
    await expect(page).toHaveURL(/\/verify-email$/);
    expect((await call<Me>("/auth/me", { token: account.token })).email_verified).toBe(true);

    await page.getByRole("button", { name: "Continue" }).click();
    await expect(composer(page)).toBeVisible();
  });

  test("a link without a code offers a fresh one", async ({ accountPage: page, account }) => {
    test.skip(!mailAvailable(), "needs the stack's mail log (E2E_API_LOG)");
    await waitForMail(account.email, /Verify your email/);
    const sent = mailCount(account.email, /Verify your email/);
    await page.goto("/verify-email");
    await expect(page.getByRole("heading", { name: "Verify your email" })).toBeVisible();
    await expect(page.getByText(/That link has expired\./)).toBeVisible();
    await page.getByRole("button", { name: "Resend" }).click();
    await expect(page.getByText("Sent. Check your email.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Resend" })).toBeHidden();
    // The resend is a real email with a fresh code, not only a message on the page.
    const fresh = await waitForMail(account.email, /Verify your email/, { after: sent });
    expect(codeIn(fresh, "ev_")).toMatch(/^ev_/);
  });

  test("a code the server never issued is treated as expired", async ({
    accountPage: page,
    account,
  }) => {
    await page.goto("/verify-email#ev_thisCodeWasNeverIssuedByTheServer");
    await expect(page.getByRole("heading", { name: "Verify your email" })).toBeVisible();
    await expect(page.getByText(/That link has expired\./)).toBeVisible();
    await expect(page.getByRole("button", { name: "Resend" })).toBeVisible();
    expect((await call<Me>("/auth/me", { token: account.token })).email_verified).toBe(false);
  });

  test("signed out, the link waits through sign-in and then verifies", async ({
    page,
    account,
  }) => {
    test.skip(!mailAvailable(), "needs the stack's mail log (E2E_API_LOG)");
    const code = codeIn(await waitForMail(account.email, /Verify your email/), "ev_");
    await page.goto(`/verify-email#${code}`);
    await expect(page).toHaveURL(/\/login\?next=%2Fverify-email$/);
    await page.getByRole("textbox", { name: "Email" }).fill(account.email);
    await page.getByRole("textbox", { name: "Password" }).fill(account.password);
    await page.getByRole("button", { name: "Sign in" }).click();

    await expect(page.getByRole("heading", { name: "Email verified" })).toBeVisible();
    expect((await call<Me>("/auth/me", { token: account.token })).email_verified).toBe(true);
  });
});
