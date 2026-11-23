/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { expect, knownBug, test } from "./fixtures";

// Allowed sites: the hosts the computer's browser opens without asking. A fresh account starts
// with none, and every change is the server's (a reload reads it back).
test.describe("allowed sites", () => {
  test("adds a host, keeps it, and revokes it", async ({ accountPage: page }) => {
    await page.goto("/allowed-sites");
    await expect(page.getByText("No always-allowed sites.")).toBeVisible();

    const host = page.getByRole("textbox", { name: "Host" });
    const allow = page.getByRole("button", { name: "Always allow" });
    await expect(allow).toBeDisabled();
    await host.fill("docs.example.org");
    await allow.click();

    const main = page.getByRole("main");
    await expect(main.getByText("docs.example.org")).toBeVisible();
    await expect(main.getByText("added here")).toBeVisible();
    await expect(host).toHaveValue("");

    await page.reload();
    await expect(main.getByText("docs.example.org")).toBeVisible();
    await main.getByRole("button", { name: "Revoke" }).click();
    await expect(page.getByText("No always-allowed sites.")).toBeVisible();
    await page.reload();
    await expect(page.getByText("No always-allowed sites.")).toBeVisible();
  });

  test("refuses what is not a public host name", async ({ accountPage: page }) => {
    await page.goto("/allowed-sites");
    const host = page.getByRole("textbox", { name: "Host" });
    const main = page.getByRole("main");
    // The refusal is the server's reason, inline beside the field, and nothing is added.
    for (const [typed, reason] of [
      ["localhost", /localhost is never askable/],
      ["https://docs.example.org/page", /a host, not a URL/],
      ["*.example.org", /wildcards are not allowed/],
    ] as const) {
      await host.fill(typed);
      await page.getByRole("button", { name: "Always allow" }).click();
      await expect(main.getByText(reason)).toBeVisible();
    }
    await expect(page.getByText("No always-allowed sites.")).toBeVisible();
  });

  test("adding a host that is already allowed lists it once", async ({ accountPage: page }) => {
    knownBug(
      "allowed-sites/route.tsx prepends the row the API returns even when the host was already " +
        "allowed (the API answers with the existing row), so the host shows twice until a reload",
    );
    await page.goto("/allowed-sites");
    const host = page.getByRole("textbox", { name: "Host" });
    for (let i = 0; i < 2; i++) {
      await host.fill("status.example.org");
      await page.getByRole("button", { name: "Always allow" }).click();
      await expect(host).toHaveValue("");
    }
    await expect(page.getByRole("main").getByText("status.example.org")).toHaveCount(1);
  });
});
