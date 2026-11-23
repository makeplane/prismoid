/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { signUp } from "../support/accounts";
import { call } from "../support/api";
import { expect, test } from "../fixtures";

// The operator console's front door: its own app, whose login refuses any account without the admin
// scope, and whose shell re-proves the scope on every load. The seeded operator's session is shared
// by every console flow, so the sign-out here ends a session this flow opened itself, never that one.
test.describe("operator console sign-in", () => {
  test("a signed-out visit lands on the console's own sign-in", async ({ page }) => {
    await page.goto("/");
    await expect(page).toHaveURL(/\/login/);
    await expect(page.getByRole("heading", { name: "Operator console", level: 1 })).toBeVisible();
    await expect(page.getByText("Sign in with an admin account to manage Prismoid.")).toBeVisible();
    await expect(page.getByText("Admin accounts only")).toBeVisible();
  });

  test("refuses an account without the admin scope, and leaves no session behind", async ({
    page,
    seed,
  }) => {
    test.skip(!seed.admin, "no operator account");
    const person = await signUp("e2e-admin-login");
    await page.goto("/login");
    await page.getByRole("textbox", { name: "Email" }).fill(person.email);
    await page.getByRole("textbox", { name: "Password" }).fill(person.password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(
      page.getByText("This console is for operators. Your account doesn't hold the admin scope."),
    ).toBeVisible();
    await expect(page).toHaveURL(/\/login/);

    // The refused sign-in opened a session to ask who this is; it must be signed out again, so the
    // only live session left is the one signup made.
    const seen = await call<{ live_sessions: number }>(`/admin/users/${person.id}`, {
      token: seed.admin!.token,
    });
    expect(seen.live_sessions).toBe(1);

    await page.goto("/");
    await expect(page).toHaveURL(/\/login/);
  });

  test("a wrong password is refused", async ({ page, seed }) => {
    test.skip(!seed.admin, "no operator account");
    await page.goto("/login");
    await page.getByRole("textbox", { name: "Email" }).fill(seed.admin!.email);
    await page.getByRole("textbox", { name: "Password" }).fill("not the operator password");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByText("Invalid email or password.")).toBeVisible();
  });

  test("the operator signs in through the form, and signing out ends only that session", async ({
    page,
    seed,
  }) => {
    test.skip(!seed.admin, "no operator account");
    const operator = seed.admin!;
    await page.goto("/login");
    await page.getByRole("textbox", { name: "Email" }).fill(operator.email);
    await page.getByRole("textbox", { name: "Password" }).fill(operator.password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole("heading", { name: "Health" })).toBeVisible();

    // The sidebar's footer names the operator and signs this browser out in one click.
    await page
      .getByRole("button", { name: new RegExp(operator.email.replace(/[.+]/g, "\\$&")) })
      .click();
    await expect(page).toHaveURL(/\/login/);
    await page.goto("/");
    await expect(page).toHaveURL(/\/login/);

    // The run's shared operator session is a different one, and it is still live.
    const me = await call<{ email: string; is_admin: boolean }>("/auth/me", {
      token: operator.token,
    });
    expect(me).toMatchObject({ email: operator.email, is_admin: true });
  });

  test("every sidebar section opens its page", async ({ adminPage: page }) => {
    await page.goto("/");
    const sections: [string, RegExp, string][] = [
      ["Users", /\/users$/, "Users · Admin · Prismoid"],
      ["Workspaces", /\/workspaces$/, "Workspaces · Admin · Prismoid"],
      ["Projects", /\/projects$/, "Projects · Admin · Prismoid"],
      ["Tasks", /\/tasks$/, "Tasks · Admin · Prismoid"],
      ["Bots", /\/bots$/, "Bots · Admin · Prismoid"],
      ["Routines", /\/routines$/, "Routines · Admin · Prismoid"],
      ["Models", /\/models$/, "Models · Admin · Prismoid"],
      ["Spend", /\/spend$/, "Spend · Admin · Prismoid"],
      ["Audit", /\/audit$/, "Audit · Admin · Prismoid"],
      ["Overview", /:\d+\/$/, "Admin · Prismoid"],
    ];
    for (const [label, url, title] of sections) {
      await page.getByRole("link", { name: label, exact: true }).click();
      await expect(page).toHaveURL(url);
      await expect(page).toHaveTitle(title);
    }
    // Each page shows its own content, not just a title: spot-check the landmarks.
    await expect(page.getByRole("heading", { name: "Health" })).toBeVisible();
    await page.getByRole("link", { name: "Users", exact: true }).click();
    await expect(page.getByRole("textbox", { name: "Search email, name, or ID…" })).toBeVisible();
    await page.getByRole("link", { name: "Tasks", exact: true }).click();
    await expect(
      page.getByRole("textbox", { name: "Search title, goal, or task ID…" }),
    ).toBeVisible();
    await page.getByRole("link", { name: "Bots", exact: true }).click();
    await expect(page.getByRole("textbox", { name: "Search bot name…" })).toBeVisible();
    await page.getByRole("link", { name: "Routines", exact: true }).click();
    await expect(page.getByRole("textbox", { name: "Search routine names…" })).toBeVisible();
    await page.getByRole("link", { name: "Models", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Doctor" })).toBeVisible();
    await page.getByRole("link", { name: "Spend", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Daily" })).toBeVisible();
  });
});
