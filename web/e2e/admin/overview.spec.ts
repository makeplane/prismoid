/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { expect, test } from "../fixtures";

// The console's landing: the worker's health, what is asking for an operator, and spend at a glance.
// The e2e stack holds no provider key, so the catalog doctor always has something to say: every
// enabled model will 401 mid-run, and the Alerts must say so rather than "All quiet".
test.describe("console overview", () => {
  test("shows health, the catalog doctor's alert, and spend", async ({ adminPage: page }) => {
    await page.goto("/");
    const main = page.getByRole("main");
    await expect(main.getByRole("heading", { name: "Health", level: 2 })).toBeVisible();
    await expect(main.getByText(/worker ok/)).toBeVisible();
    await expect(main.getByText(/migration /)).toBeVisible();

    await expect(main.getByRole("heading", { name: "Alerts", level: 2 })).toBeVisible();
    const alert = main.getByRole("listitem").filter({ hasText: "catalog-doctor" });
    await expect(alert).toBeVisible();
    await expect(alert).toContainText("warn");
    await expect(alert).toContainText("key-missing");
    await expect(main.getByText("All quiet")).toHaveCount(0);

    await expect(main.getByRole("heading", { name: "Spend", level: 2 })).toBeVisible();
    await expect(
      main.getByText(/today · .* this month · .* over 30 days · \d+ calls/),
    ).toBeVisible();
    await main.getByRole("link", { name: "Full breakdown" }).click();
    await expect(page).toHaveURL(/\/spend$/);
    await expect(page.getByRole("heading", { name: "Daily" })).toBeVisible();
  });
});
