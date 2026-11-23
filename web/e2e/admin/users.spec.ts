/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { Page } from "@playwright/test";
import { type Account, signIn, signUp } from "../support/accounts";
import { ApiError, call, stamp } from "../support/api";
import { escapeRegExp, expect, test, toast } from "../fixtures";

// The operator's verbs on a person: suspend, grant or revoke the console, sign out everywhere, and
// revoke one credential at a time. Each flow acts on an account it signs up itself (the user list is
// global and holds every run's accounts), never on the seeded operator.

/** A fresh person, with a name of their own so their row and header are unmistakable. */
function target(): Promise<Account> {
  return signUp("e2e-admin-users", { name: `Una Target ${stamp()}` });
}

/** The status a credential gets from the API: 200 while it lives, the refusal's code after. */
async function statusOf(token: string): Promise<number> {
  try {
    await call("/auth/me", { token });
    return 200;
  } catch (e) {
    if (e instanceof ApiError) return e.status;
    throw e;
  }
}

/** The person's audit trail, read on their History tab. */
async function history(page: Page) {
  await page.getByRole("tab", { name: "History" }).click();
  return page.getByRole("main").getByRole("listitem");
}

test.describe("console users", () => {
  test("search finds a fresh account and its page has four tabs", async ({ adminPage: page }) => {
    const person = await target();
    await page.goto("/users");
    await page.getByRole("textbox", { name: "Search email, name, or ID…" }).fill(person.email);
    const main = page.getByRole("main");
    const row = main.getByRole("link", { name: new RegExp(escapeRegExp(person.email)) });
    await expect(row).toHaveCount(1);
    await expect(row).toContainText(person.name);
    await expect(row).toContainText("active");

    // The search is in the URL, so a reload runs it fresh; every user row says when they joined,
    // and this account's is the only one left.
    await page.reload();
    await expect(row).toBeVisible();
    await expect(main.getByRole("link", { name: / · joined / })).toHaveCount(1);
    await row.click();
    await expect(page).toHaveURL(new RegExp(`/users/${person.id}$`));
    await expect(main.getByRole("heading", { name: person.name, level: 1 })).toBeVisible();
    await expect(main.getByText(person.email).first()).toBeVisible();
    for (const name of ["Overview", "Tasks", "Access", "History"])
      await expect(main.getByRole("tab", { name })).toBeVisible();
    await expect(main.getByRole("heading", { name: "Status & actions" })).toBeVisible();
    await expect(main.getByText("1 live session · 0 API keys")).toBeVisible();

    await main.getByRole("tab", { name: "Tasks" }).click();
    await expect(page).toHaveURL(/tab=tasks/);
    await expect(main.getByText("No tasks match the current filters.")).toBeVisible();
    await main.getByRole("tab", { name: "History" }).click();
    await expect(main.getByText("No admin actions on this account yet")).toBeVisible();
  });

  test("suspend locks the person out, and unsuspend lets them sign in again", async ({
    adminPage: page,
  }) => {
    const person = await target();
    await page.goto(`/users/${person.id}`);
    const main = page.getByRole("main");
    await main.getByRole("button", { name: "Suspend" }).click();

    const dialog = page.getByRole("dialog", { name: `Suspend ${person.email}?` });
    const confirm = dialog.getByRole("button", { name: "Suspend" });
    // The reason is the audit row's whole point: no reason, no suspension.
    await expect(confirm).toBeDisabled();
    await dialog.getByRole("textbox", { name: "Reason" }).fill("Card testing from this account");
    await confirm.click();
    await expect(toast(page, "Suspended.")).toBeVisible();
    await expect(dialog).toBeHidden();
    await expect(main.getByText("suspended", { exact: true })).toBeVisible();
    await expect(main.getByRole("button", { name: "Unsuspend" })).toBeVisible();

    // Locked out: the session they had is dead, and the password no longer opens a new one.
    expect(await statusOf(person.token)).toBe(401);
    await expect(signIn(person)).rejects.toMatchObject({ status: 403 });

    // The status chips slice the list by it.
    await page.goto(`/users?q=${encodeURIComponent(person.email)}`);
    const row = main.getByRole("link", { name: new RegExp(escapeRegExp(person.email)) });
    await expect(row).toContainText("suspended");
    await main.getByRole("button", { name: "Suspended", exact: true }).click();
    await expect(page).toHaveURL(/status=suspended/);
    await expect(row).toBeVisible();
    await main.getByRole("button", { name: "Active", exact: true }).click();
    await expect(main.getByText(`No matches for “${person.email}”`)).toBeVisible();
    await main.getByRole("button", { name: "All", exact: true }).click();
    await expect(row).toBeVisible();

    await row.click();
    await main.getByRole("button", { name: "Unsuspend" }).click();
    const unsuspend = page.getByRole("alertdialog", { name: `Unsuspend ${person.email}?` });
    await unsuspend.getByRole("button", { name: "Unsuspend" }).click();
    await expect(toast(page, "Unsuspended.")).toBeVisible();
    await expect(main.getByText("active", { exact: true })).toBeVisible();
    await expect(main.getByRole("button", { name: "Suspend" })).toBeVisible();
    // Back in, with a new sign-in; the session suspension killed stays dead.
    const fresh = await signIn(person);
    expect(await statusOf(fresh.token)).toBe(200);
    expect(await statusOf(person.token)).toBe(401);

    const rows = await history(page);
    await expect(rows.filter({ hasText: "user.suspend" })).toContainText(
      "Card testing from this account",
    );
    await expect(rows.filter({ hasText: "user.unsuspend" })).toHaveCount(1);
  });

  test("grants the console to an account and takes it back", async ({ adminPage: page, seed }) => {
    const person = await target();
    try {
      await page.goto(`/users/${person.id}`);
      const main = page.getByRole("main");
      await main.getByRole("button", { name: "Grant admin" }).click();
      const grant = page.getByRole("alertdialog", { name: `Make ${person.email} an admin?` });
      await expect(grant).toContainText("Admins see every user, task, and ledger row.");
      await grant.getByRole("button", { name: "Grant admin" }).click();
      await expect(toast(page, "Admin granted.")).toBeVisible();
      await expect(main.getByText("admin", { exact: true })).toBeVisible();
      const granted = await call<{ is_admin: boolean }>("/auth/me", { token: person.token });
      expect(granted.is_admin).toBe(true);

      await main.getByRole("button", { name: "Revoke admin" }).click();
      const revoke = page.getByRole("alertdialog", { name: `Revoke admin from ${person.email}?` });
      await revoke.getByRole("button", { name: "Revoke admin" }).click();
      await expect(toast(page, "Admin revoked.")).toBeVisible();
      await expect(main.getByRole("button", { name: "Grant admin" })).toBeVisible();
      await expect(main.getByText("admin", { exact: true })).toHaveCount(0);
      const revoked = await call<{ is_admin: boolean }>("/auth/me", { token: person.token });
      expect(revoked.is_admin).toBe(false);

      const rows = await history(page);
      await expect(rows.filter({ hasText: "admin.grant" })).toHaveCount(1);
      await expect(rows.filter({ hasText: "admin.revoke" })).toHaveCount(1);
    } finally {
      // A failed run must not leave a stray operator behind.
      await call(`/admin/users/${person.id}/revoke-admin`, {
        token: seed.admin!.token,
        body: {},
      }).catch(() => undefined);
    }
  });

  test("sign out everywhere ends every session and API key", async ({ adminPage: page }) => {
    const person = await target();
    const second = await signIn(person);
    const key = await call<{ key: string }>("/auth/api-keys", {
      token: person.token,
      body: { name: "deploy bot" },
    });
    expect(await statusOf(key.key)).toBe(200);

    await page.goto(`/users/${person.id}`);
    const main = page.getByRole("main");
    await expect(main.getByText("2 live sessions · 1 API key")).toBeVisible();
    await main.getByRole("button", { name: "Sign out everywhere" }).click();
    const dialog = page.getByRole("alertdialog", {
      name: `Revoke every credential for ${person.email}?`,
    });
    await dialog.getByRole("button", { name: "Sign out everywhere" }).click();
    await expect(toast(page, "Revoked 2 sessions and 1 API key.")).toBeVisible();
    await expect(main.getByText("0 live sessions · 0 API keys")).toBeVisible();

    for (const token of [person.token, second.token, key.key])
      expect(await statusOf(token)).toBe(401);
    const rows = await history(page);
    await expect(rows.filter({ hasText: "user.revoke_sessions" })).toHaveCount(1);
  });

  test("the Access tab revokes one session and one API key", async ({ adminPage: page }) => {
    const person = await target();
    const second = await signIn(person);
    const keyName = `ci key ${stamp()}`;
    const key = await call<{ key: string }>("/auth/api-keys", {
      token: person.token,
      body: { name: keyName },
    });

    await page.goto(`/users/${person.id}?tab=access`);
    const main = page.getByRole("main");
    const sessions = main.getByRole("list").filter({ hasText: "signed in" }).getByRole("listitem");
    await expect(sessions).toHaveCount(2);
    await expect(sessions.filter({ hasText: "live" })).toHaveCount(2);

    await sessions.first().getByRole("button", { name: "Revoke" }).click();
    const one = page.getByRole("alertdialog", { name: "Revoke this session?" });
    await one.getByRole("button", { name: "Revoke" }).click();
    await expect(toast(page, "Session revoked.")).toBeVisible();
    await expect(sessions.filter({ hasText: "revoked" })).toHaveCount(1);
    await expect(sessions.getByRole("button", { name: "Revoke" })).toHaveCount(1);
    // Exactly one of the two sessions ended; the other still works.
    const alive = [await statusOf(person.token), await statusOf(second.token)].sort();
    expect(alive).toEqual([200, 401]);

    const keyRow = main.getByRole("listitem").filter({ hasText: keyName });
    await expect(keyRow).toContainText("never used");
    await keyRow.getByRole("button", { name: "Revoke" }).click();
    const revokeKey = page.getByRole("alertdialog", { name: `Revoke the key “${keyName}”?` });
    await revokeKey.getByRole("button", { name: "Revoke" }).click();
    await expect(toast(page, "API key revoked.")).toBeVisible();
    await expect(keyRow).toContainText("revoked");
    await expect(keyRow.getByRole("button", { name: "Revoke" })).toHaveCount(0);
    expect(await statusOf(key.key)).toBe(401);

    // The overview's counts follow the revokes.
    await page.reload();
    await main.getByRole("tab", { name: "Overview" }).click();
    await expect(main.getByText("1 live session · 0 API keys")).toBeVisible();

    const rows = await history(page);
    await expect(rows.filter({ hasText: "user.revoke_session" })).toHaveCount(1);
    await expect(rows.filter({ hasText: "user.revoke_api_key" })).toHaveCount(1);
  });
});
