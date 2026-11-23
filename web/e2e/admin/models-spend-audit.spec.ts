/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { signUp } from "../support/accounts";
import { call, stamp } from "../support/api";
import { escapeRegExp, expect, test, toast } from "../fixtures";

// The model catalog, the ledger and the audit trail. The catalog and the price book are global, so
// a flow only ever changes a model it added itself (a stamped id under a made-up provider): it
// leaves every seeded model, price and route exactly as it found them. An added model starts
// disabled, and the flow leaves it disabled again whatever happens.

test.describe("console models", () => {
  test("the doctor names every model whose provider has no key", async ({ adminPage: page }) => {
    await page.goto("/models");
    const main = page.getByRole("main");
    await expect(main.getByRole("heading", { name: "Doctor", level: 2 })).toBeVisible();
    await expect(main.getByText("Catalog healthy")).toHaveCount(0);
    // The e2e stack holds no provider key, so the seeded Anthropic models are among the findings.
    const finding = main
      .getByRole("listitem")
      .filter({ hasText: "key-missing" })
      .filter({ hasText: "anthropic:claude-opus-5-5" });
    await expect(finding).toContainText(
      "no API key resolves for provider 'anthropic' - every call will 401 mid-run.",
    );
    await expect(finding).toContainText("PRISMOID_GATEWAY_ANTHROPIC_API_KEY");
    for (const section of ["Catalog", "Routing table"])
      await expect(main.getByRole("heading", { name: new RegExp(`^${section}`) })).toBeVisible();
  });

  test("adding a model needs a provider, a model id, a family and a class", async ({
    adminPage: page,
  }) => {
    await page.goto("/models");
    await page.getByRole("main").getByRole("button", { name: "Add model" }).click();
    const dialog = page.getByRole("dialog", { name: "Add a model" });
    await dialog.getByRole("textbox", { name: "Provider" }).fill("e2e");
    await dialog.getByRole("button", { name: "Add model" }).click();
    await expect(
      toast(page, "Provider, model id, family, and at least one class are required."),
    ).toBeVisible();
    await expect(dialog).toBeVisible();
  });

  test("adds a model, switches it on and off, and reprices it", async ({
    adminPage: page,
    seed,
  }) => {
    const modelId = `probe-${stamp()}`;
    const ref = `e2e:${modelId}`;
    const main = page.getByRole("main");
    try {
      await page.goto("/models");
      await main.getByRole("button", { name: "Add model" }).click();
      const dialog = page.getByRole("dialog", { name: "Add a model" });
      await dialog.getByRole("textbox", { name: "Provider" }).fill("e2e");
      await dialog.getByRole("textbox", { name: "Model id" }).fill(modelId);
      await dialog.getByRole("textbox", { name: "Family" }).fill("e2e");
      await dialog.getByRole("textbox", { name: "Context window" }).fill("8000");
      await dialog.getByRole("button", { name: "cheap-classifier", exact: true }).click();
      await dialog.getByRole("textbox", { name: "input_token" }).fill("0.000001");
      await dialog.getByRole("textbox", { name: "output_token" }).fill("0.000002");
      await dialog.getByRole("button", { name: "Add model" }).click();
      await expect(
        toast(page, "Model added, disabled, and priced. Enable it when you're ready."),
      ).toBeVisible();
      await expect(dialog).toBeHidden();

      // A new model arrives switched off.
      const catalogRow = main
        .getByRole("listitem")
        .filter({ has: page.getByRole("switch", { name: new RegExp(`${ref}$`) }) });
      await expect(catalogRow).toContainText("cheap-classifier");
      await expect(catalogRow).toContainText("8K ctx");
      const enable = main.getByRole("switch", { name: `Enable ${ref}` });
      const disable = main.getByRole("switch", { name: `Disable ${ref}` });
      await expect(enable).not.toBeChecked();

      await enable.click();
      await expect(disable).toBeChecked();
      await page.reload();
      await expect(disable).toBeChecked();
      await disable.click();
      await expect(enable).not.toBeChecked();
      await page.reload();
      await expect(enable).not.toBeChecked();

      // The price book: a reprice mints a new version and changes only this model.
      const book = main.getByRole("heading", { name: /^Price book · v\d+$/ });
      const before = Number((await book.textContent())!.match(/v(\d+)/)![1]);
      const priceRow = main
        .getByRole("listitem")
        .filter({ hasText: ref })
        .filter({ has: page.getByRole("button", { name: "Edit prices" }) });
      await expect(priceRow).toContainText("input_token = ");
      await priceRow.getByRole("button", { name: "Edit prices" }).click();
      const reprice = page.getByRole("dialog", { name: `Reprice ${ref}` });
      const save = reprice.getByRole("button", { name: "Save new version" });
      await expect(save).toBeDisabled();
      await reprice.getByRole("textbox", { name: "input_token" }).fill("0.0000015");
      await save.click();
      const saved = toast(
        page,
        new RegExp(`^Price book v(\\d+) - ${escapeRegExp(ref)} repriced\\.$`),
      );
      await expect(saved).toBeVisible();
      const after = Number((await saved.textContent())!.match(/v(\d+)/)![1]);
      expect(after).toBeGreaterThan(before);
      await expect(main.getByRole("heading", { name: `Price book · v${after}` })).toBeVisible();
      await page.reload();
      await expect(priceRow).toContainText(/input_token = 0\.0000015/);
      await expect(priceRow).toContainText(/output_token = 0\.000002/);

      // Each change is on the audit trail, against the model.
      await page.goto("/audit");
      const trail = page
        .getByRole("main")
        .getByRole("listitem")
        .filter({ hasText: `model:${ref}` });
      await expect(trail.filter({ hasText: "model.add" })).toHaveCount(1);
      await expect(trail.filter({ hasText: "model.patch" })).toHaveCount(2);
    } finally {
      // Whatever failed above, the model this flow added is left switched off.
      await call(`/admin/models/${encodeURIComponent(ref)}`, {
        method: "PATCH",
        token: seed.admin!.token,
        body: { enabled: false },
      }).catch(() => undefined);
    }
  });
});

test.describe("console spend", () => {
  test("shows the ledger's tiles and slices the daily chart", async ({ adminPage: page }) => {
    await page.goto("/spend");
    const main = page.getByRole("main");
    for (const tile of ["Last 30 days", "This month", "Today", "Calls"])
      await expect(main.getByText(tile, { exact: true })).toBeVisible();
    await expect(main.getByRole("heading", { name: "Daily", level: 2 })).toBeVisible();
    // "By model" is both a slice of the chart and a section of its own.
    await expect(main.getByRole("heading", { name: "By model", level: 2 })).toBeVisible();
    await expect(main.getByRole("heading", { name: "Top spenders", level: 2 })).toBeVisible();

    const total = main.getByRole("button", { name: "Total", exact: true });
    const byModel = main.getByRole("button", { name: "By model", exact: true });
    const byUser = main.getByRole("button", { name: "By user", exact: true });
    await expect(total).toHaveAttribute("aria-pressed", "true");
    await byModel.click();
    await expect(page).toHaveURL(/by=model/);
    await expect(byModel).toHaveAttribute("aria-pressed", "true");
    await expect(total).toHaveAttribute("aria-pressed", "false");
    await byUser.click();
    await expect(page).toHaveURL(/by=user/);
    await expect(byUser).toHaveAttribute("aria-pressed", "true");

    // The slice is in the URL, so a pasted link opens the same view.
    await page.reload();
    await expect(byUser).toHaveAttribute("aria-pressed", "true");
    await total.click();
    await expect(page).not.toHaveURL(/by=/);
    await expect(total).toHaveAttribute("aria-pressed", "true");
  });
});

test.describe("console audit", () => {
  test("lists an operator's act with its reason and links to the subject", async ({
    adminPage: page,
    seed,
  }) => {
    const person = await signUp("e2e-admin-audit");
    const reason = `Chargeback under review ${stamp()}`;
    // The act happens through the API as the run's operator, so this flow reads only the trail.
    await call(`/admin/users/${person.id}/suspend`, { token: seed.admin!.token, body: { reason } });
    await call(`/admin/users/${person.id}/unsuspend`, { token: seed.admin!.token, body: {} });

    await page.goto("/audit");
    const rows = page.getByRole("main").getByRole("listitem");
    const suspended = rows
      .filter({ hasText: "user.suspend" })
      .filter({ has: page.getByRole("link", { name: `user:${person.id}` }) });
    await expect(suspended).toContainText(reason);
    await expect(suspended).toContainText(seed.admin!.email);
    await expect(
      rows
        .filter({ hasText: "user.unsuspend" })
        .filter({ has: page.getByRole("link", { name: `user:${person.id}` }) }),
    ).toHaveCount(1);

    await suspended.getByRole("link", { name: `user:${person.id}` }).click();
    await expect(page).toHaveURL(new RegExp(`/users/${person.id}$`));
  });
});
