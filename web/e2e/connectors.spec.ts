/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { expect, openSettings, test } from "./fixtures";
import { stamp } from "./support/api";

// Connectors: the catalog and custom MCP servers. No flow here ever connects a catalog entry or
// retries a probe: those dial real hosts. A custom server lives under the .invalid name, which
// never resolves, so its one probe fails at once and nothing leaves the machine.
test.describe("connectors", () => {
  test("the lens chips filter the catalog and ride the URL", async ({ accountPage: page }) => {
    await page.goto("/connectors");
    const main = page.getByRole("main");
    const chip = (label: string) => main.getByRole("button", { name: label, exact: true });
    const notion = main.getByRole("button", { name: "Notion: available", exact: true });
    await expect(chip("All")).toHaveAttribute("aria-pressed", "true");
    await expect(notion).toBeVisible();

    await chip("Connected").click();
    await expect(page).toHaveURL(/\?filter=connected$/);
    await expect(chip("Connected")).toHaveAttribute("aria-pressed", "true");
    await expect(chip("All")).toHaveAttribute("aria-pressed", "false");
    // A fresh account has connected nothing.
    await expect(main.getByText("No connectors here")).toBeVisible();
    await expect(notion).toHaveCount(0);

    await chip("Available").click();
    await expect(page).toHaveURL(/\?filter=available$/);
    await expect(notion).toBeVisible();
    await page.reload();
    await expect(chip("Available")).toHaveAttribute("aria-pressed", "true");

    await chip("All").click();
    await expect(page).toHaveURL(/\/connectors$/);
  });

  test("search and the category menu narrow the catalog", async ({ accountPage: page }) => {
    await page.goto("/connectors");
    const main = page.getByRole("main");
    const card = (name: string) =>
      main.getByRole("button", { name: `${name}: available`, exact: true });
    const search = main.getByRole("textbox", { name: "Search connectors…" });

    await search.fill("linear");
    await expect(card("Linear")).toBeVisible();
    await expect(card("Notion")).toHaveCount(0);
    await search.fill("zzz-no-such-service");
    await expect(main.getByText("No matches for “zzz-no-such-service”")).toBeVisible();
    await search.fill("");
    await expect(card("Notion")).toBeVisible();

    await main.getByRole("button", { name: "Filter by category" }).click();
    const menu = page.getByRole("menu", { name: "Filter by category" });
    await expect(menu.getByRole("menuitemradio", { name: "Any" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    await menu.getByRole("menuitemradio", { name: "Databases" }).click();
    await expect(page).toHaveURL(/\?category=Databases$/);
    await expect(card("Neon")).toBeVisible();
    await expect(card("Supabase")).toBeVisible();
    await expect(card("Notion")).toHaveCount(0);
    // The trigger wears the category it filters by.
    const trigger = main.getByRole("button", { name: "Filter by category" });
    await expect(trigger).toContainText("Databases");

    await page.reload();
    await expect(card("Neon")).toBeVisible();
    await expect(card("Notion")).toHaveCount(0);
    await trigger.click();
    await page.getByRole("menuitemradio", { name: "Any" }).click();
    await expect(page).toHaveURL(/\/connectors$/);
    await expect(card("Notion")).toBeVisible();
  });

  test("a catalog card opens its details", async ({ accountPage: page }) => {
    await page.goto("/connectors");
    const main = page.getByRole("main");
    const card = main.getByRole("button", { name: "Notion: available", exact: true });
    // The card's name, not its centre: the card also holds the Connect button, which must
    // never be pressed here.
    await card.getByText("Notion", { exact: true }).click();
    await expect(page).toHaveURL(/\?connector=notion$/);

    const modal = page.getByRole("dialog", { name: "Notion" });
    await expect(modal.getByText("Notion · Productivity")).toBeVisible();
    await expect(
      modal.getByText("Search, read, and create pages and databases in your Notion workspace."),
    ).toBeVisible();
    await expect(modal.getByRole("button", { name: "Docs" })).toHaveAttribute(
      "href",
      /^https:\/\//,
    );
    await modal.getByRole("button", { name: "Close" }).click();
    await expect(modal).toBeHidden();
    await expect(page).toHaveURL(/\/connectors$/);

    // The URL is the modal's address: opening it cold shows the same face.
    await page.goto("/connectors?connector=notion");
    await expect(page.getByRole("dialog", { name: "Notion" })).toBeVisible();
  });

  test("adds a custom server, disables and enables it, and removes it", async ({
    accountPage: page,
    account,
  }) => {
    const tag = stamp();
    const name = `Custom ${tag}`;
    await page.goto("/connectors");
    const main = page.getByRole("main");
    await main.getByRole("button", { name: "Custom connector" }).click();
    const dialog = page.getByRole("dialog", { name: "Add a custom connector" });
    const add = dialog.getByRole("button", { name: "Add", exact: true });
    await expect(add).toBeDisabled();
    await dialog
      .getByRole("textbox", { name: "Server URL" })
      .fill(`https://mcp-${tag}.invalid/mcp`);
    await dialog.getByRole("textbox", { name: "Name (optional)" }).fill(name);
    await add.click();

    // The add waits on its one probe, which fails on the unresolvable name: the row is kept,
    // in error, and the modal opens on it.
    const modal = page.getByRole("dialog", { name, exact: true });
    await expect(modal).toBeVisible({ timeout: 30_000 });
    await expect(page).toHaveURL(/\?connector=[0-9a-f-]+$/);
    await expect(modal.getByText(`https://mcp-${tag}.invalid/mcp`)).toBeVisible();
    await expect(modal.getByText("Error", { exact: true })).toBeVisible();
    await expect(modal.getByText(/does not resolve/)).toBeVisible();

    await modal.getByRole("button", { name: "Disable" }).click();
    await expect(modal.getByText("Disabled", { exact: true })).toBeVisible();
    await modal.getByRole("button", { name: "Enable" }).click();
    await expect(modal.getByText("Error", { exact: true })).toBeVisible();
    await expect(modal.getByRole("button", { name: "Disable" })).toBeVisible();
    await modal.getByRole("button", { name: "Close" }).click();

    // Your own rows are what "Connected" lists, whatever their state.
    const row = main.getByRole("button", { name: `${name}: Error`, exact: true });
    await main.getByRole("button", { name: "Connected", exact: true }).click();
    await expect(row).toBeVisible();
    await page.reload();
    await expect(row).toBeVisible();

    // Settings keeps a roster of the same rows.
    const settings = await openSettings(page, account.email);
    await settings.getByRole("navigation").getByRole("button", { name: "Connectors" }).click();
    await expect(settings.getByRole("listitem").filter({ hasText: name })).toContainText("Error");
    await page.keyboard.press("Escape");
    await expect(settings).toBeHidden();

    await row.getByText(name, { exact: true }).click();
    await modal.getByRole("button", { name: `Remove ${name}` }).click();
    await expect(modal).toBeHidden();
    await expect(main.getByText("No connectors here")).toBeVisible();
    await page.reload();
    await expect(main.getByText("No connectors here")).toBeVisible();
  });

  test("refuses a server inside the network and adds nothing", async ({ accountPage: page }) => {
    await page.goto("/connectors?filter=connected");
    const main = page.getByRole("main");
    await main.getByRole("button", { name: "Custom connector" }).click();
    const dialog = page.getByRole("dialog", { name: "Add a custom connector" });
    const url = dialog.getByRole("textbox", { name: "Server URL" });
    // The refusal is the server's reason, said where the person is; the dialog stays open.
    // (Loopback is refused because this stack keeps the dev-only loopback setting off.)
    for (const [typed, reason] of [
      ["https://10.0.0.1/mcp", "that address points inside the network, not at a service"],
      ["https://127.0.0.1:9/mcp", "loopback MCP servers are a dev-only setting"],
      ["http://127.0.0.1:9/mcp", /^MCP servers must be https/],
    ] as const) {
      await url.fill(typed);
      await dialog.getByRole("button", { name: "Add", exact: true }).click();
      await expect(page.getByRole("alert").filter({ hasText: reason })).toBeVisible();
      await expect(url).toHaveValue(typed);
    }
    await dialog.getByRole("button", { name: "Close" }).click();
    await expect(main.getByText("No connectors here")).toBeVisible();
    await page.reload();
    await expect(main.getByText("No connectors here")).toBeVisible();
  });
});
