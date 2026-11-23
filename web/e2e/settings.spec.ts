/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { Locator } from "@playwright/test";
import { call, stamp } from "./support/api";
import { accountMenu, expect, knownBug, openSettings, test, toast } from "./fixtures";

test("edits the profile name in settings and keeps it", async ({ signedInPage: page, seed }) => {
  await page.goto("/");
  const openSettings = async () => {
    await page.getByRole("button", { name: new RegExp(seed.email) }).click();
    await page.getByRole("menuitem", { name: "Settings" }).click();
    return page.getByRole("dialog", { name: "Settings" });
  };

  let settings = await openSettings();
  const first = settings.getByRole("textbox", { name: "First name" });
  const save = settings.getByRole("button", { name: "Save" });
  await expect(first).toHaveValue("Ada");
  await expect(save).toBeDisabled();
  await first.fill("Grace");
  await expect(save).toBeEnabled();
  await save.click();
  await expect(save).toBeDisabled();

  // The change is the server's now: a reload reads it back.
  await page.reload();
  settings = await openSettings();
  await expect(settings.getByRole("textbox", { name: "First name" })).toHaveValue("Grace");
});

test("switches the appearance to dark", async ({ signedInPage: page, seed }) => {
  await page.goto("/");
  await page.getByRole("button", { name: new RegExp(seed.email) }).click();
  await page.getByRole("menuitem", { name: "Settings" }).click();
  const settings = page.getByRole("dialog", { name: "Settings" });
  await settings.getByRole("button", { name: "Dark" }).click();
  await expect(settings.getByRole("button", { name: "Dark" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.keyboard.press("Escape");
  await expect(settings).toBeHidden();
});

// The flows below each take a fresh account: they change what the account keeps (its timezone,
// memories, keys, workspace name), or read an empty state the shared account no longer has.
test.describe("settings on a fresh account", () => {
  /** The rail names Account's General and Workspace's General alike; Workspace's is the second. */
  const workspaceGeneral = (settings: Locator) =>
    settings.getByRole("button", { name: "General", exact: true }).nth(1);

  test("saves the timezone and keeps it", async ({ accountPage: page, account }) => {
    await page.goto("/");
    let settings = await openSettings(page, account.email);
    const zone = settings.getByRole("combobox", { name: "Timezone" });
    await expect(zone).toHaveText("UTC");
    await zone.click();
    await page.getByRole("option", { name: "Europe/Paris", exact: true }).click();
    await expect(toast(page, "Timezone saved.")).toBeVisible();
    await expect(zone).toHaveText("Europe/Paris");

    await page.reload();
    settings = await openSettings(page, account.email);
    await expect(settings.getByRole("combobox", { name: "Timezone" })).toHaveText("Europe/Paris");
  });

  test("adds memories by hand and finds them by search", async ({ accountPage: page, account }) => {
    await page.goto("/");
    let settings = await openSettings(page, account.email);
    await settings.getByRole("button", { name: "Memory", exact: true }).click();
    await expect(settings.getByText("Nothing remembered yet")).toBeVisible();

    const add = async (text: string, kind: string) => {
      await settings.getByRole("button", { name: "Add memory" }).click();
      const dialog = page.getByRole("dialog", { name: "Add a memory" });
      await dialog.getByRole("textbox", { name: "Memory" }).fill(text);
      await dialog.getByRole("button", { name: kind, exact: true }).click();
      await dialog.getByRole("button", { name: "Add memory" }).click();
      await expect(dialog).toBeHidden();
    };
    await add("Prefers tide tables in metric units.", "preference");
    await add("The field station is on the north shore.", "fact");
    const rows = settings.getByRole("listitem");
    await expect(rows).toHaveCount(2);
    const tides = rows.filter({ hasText: "Prefers tide tables in metric units." });
    await expect(tides).toContainText("Preference");
    await expect(tides).toContainText("added by you");
    await expect(rows.filter({ hasText: "The field station" })).toContainText("Fact");

    // Search narrows the list to what matches, and says so when nothing does.
    const search = settings.getByRole("textbox", { name: "Search memories…" });
    await search.fill("north shore");
    await expect(rows).toHaveCount(1);
    await expect(rows).toContainText("The field station is on the north shore.");
    await search.fill("volcano");
    await expect(settings.getByText("No matches for “volcano”")).toBeVisible();
    await search.fill("");
    await expect(rows).toHaveCount(2);

    await page.reload();
    settings = await openSettings(page, account.email);
    await settings.getByRole("button", { name: "Memory", exact: true }).click();
    await expect(tides).toContainText("Preference");
    await expect(rows).toHaveCount(2);
  });

  test("edits a memory in place", async ({ accountPage: page, account }) => {
    knownBug(
      "backend/prismoid/api/serializers/memories.py:83 reads updated_at after " +
        "operations/memory.py:174 committed and expired it, so PATCH /v1/memories/{id} answers " +
        "500 (MissingGreenlet) although the edit is saved; the row stays open in edit mode",
    );
    await call("/memories", {
      token: account.token,
      body: { scope: "user", kind: "preference", text: "Prefers tide tables in metric units." },
    });
    await page.goto("/");
    const settings = await openSettings(page, account.email);
    await settings.getByRole("button", { name: "Memory", exact: true }).click();
    const rows = settings.getByRole("listitem");
    await rows.hover();
    await rows.getByRole("button", { name: "Edit" }).click();
    // The edit box has no label of its own, so it is the row's only textbox.
    const box = rows.getByRole("textbox");
    await expect(box).toHaveValue("Prefers tide tables in metric units.");
    await box.fill("Prefers tide tables in feet.");
    await rows.getByRole("button", { name: "Save" }).click();

    await expect(box).toBeHidden();
    await expect(rows).toContainText("Prefers tide tables in feet.");
    await page.reload();
    const reopened = await openSettings(page, account.email);
    await reopened.getByRole("button", { name: "Memory", exact: true }).click();
    await expect(reopened.getByRole("listitem")).toContainText("Prefers tide tables in feet.");
  });

  test("retires a memory", async ({ accountPage: page, account }) => {
    knownBug(
      "backend/prismoid/api/serializers/memories.py:83 reads updated_at after " +
        "operations/memory.py:186 committed and expired it, so POST /v1/memories/{id}/retire " +
        "answers 500 (MissingGreenlet) although the memory is retired; the row stays listed",
    );
    await call("/memories", {
      token: account.token,
      body: { scope: "user", kind: "fact", text: "The field station is on the north shore." },
    });
    await page.goto("/");
    const settings = await openSettings(page, account.email);
    await settings.getByRole("button", { name: "Memory", exact: true }).click();
    const row = settings.getByRole("listitem").filter({ hasText: "The field station" });
    await row.hover();
    await row.getByRole("button", { name: "Retire" }).click();

    await expect(row).toBeHidden();
    await expect(settings.getByText("Nothing remembered yet")).toBeVisible();
    await page.reload();
    const reopened = await openSettings(page, account.email);
    await reopened.getByRole("button", { name: "Memory", exact: true }).click();
    await expect(reopened.getByText("Nothing remembered yet")).toBeVisible();
  });

  test("creates an API key that works, then revokes it", async ({ accountPage: page, account }) => {
    await page.goto("/");
    const settings = await openSettings(page, account.email);
    await settings.getByRole("button", { name: "API keys", exact: true }).click();
    await expect(settings.getByText("No API keys yet")).toBeVisible();

    await settings.getByRole("button", { name: "New key" }).click();
    const dialog = page.getByRole("dialog", { name: "New API key" });
    await dialog.getByRole("textbox", { name: "Name" }).fill("Tide station CLI");
    await dialog.getByRole("button", { name: "Create key" }).click();
    const shown = page.getByRole("dialog", { name: "Copy your key now" });
    const key = (await shown.getByText(/^ck_/).textContent())?.trim() ?? "";
    expect(key).toMatch(/^ck_/);
    await shown.getByRole("button", { name: "Done" }).click();
    await expect(shown).toBeHidden();

    // The key is a real credential: the API answers it as this person.
    const me = await call<{ email: string }>("/auth/me", { token: key });
    expect(me.email).toBe(account.email);

    const row = settings.getByRole("listitem").filter({ hasText: "Tide station CLI" });
    await expect(row).toBeVisible();
    await row.getByRole("button", { name: "Revoke" }).click();
    await expect(row).toContainText("revoked");
    await expect(row.getByRole("button", { name: "Revoke" })).toBeHidden();
    await expect(call("/auth/me", { token: key })).rejects.toMatchObject({ status: 401 });
  });

  test("renames the workspace and the switcher wears the name", async ({
    accountPage: page,
    account,
  }) => {
    const name = `Tide Lab ${stamp()}`;
    const slug = `tide-lab-${stamp()}`;
    await page.goto("/");
    let settings = await openSettings(page, account.email);
    await workspaceGeneral(settings).click();
    const save = settings.getByRole("button", { name: "Save" });
    await expect(settings.getByRole("textbox", { name: "Name" })).toHaveValue(
      account.workspace.name,
    );
    await expect(save).toBeDisabled();
    await settings.getByRole("textbox", { name: "Name" }).fill(name);
    await settings.getByRole("textbox", { name: "Slug" }).fill(slug);
    await save.click();
    await expect(toast(page, "Workspace saved.")).toBeVisible();

    await page.keyboard.press("Escape");
    await expect(settings).toBeHidden();
    await expect(page.getByRole("button", { name: "Switch workspace" })).toContainText(name);

    await page.reload();
    await expect(page.getByRole("button", { name: "Switch workspace" })).toContainText(name);
    settings = await openSettings(page, account.email);
    await workspaceGeneral(settings).click();
    await expect(settings.getByRole("textbox", { name: "Name" })).toHaveValue(name);
    await expect(settings.getByRole("textbox", { name: "Slug" })).toHaveValue(slug);
  });

  test("lists the builtin skills", async ({ accountPage: page, account }) => {
    await page.goto("/");
    const settings = await openSettings(page, account.email);
    await settings.getByRole("button", { name: "Skills", exact: true }).click();
    await expect(settings.getByRole("heading", { name: "Skills" })).toBeVisible();
    for (const skill of ["analyze", "research", "report", "summarize"]) {
      await expect(settings.getByText(skill, { exact: true })).toBeVisible();
    }
  });

  test("the connectors pane starts empty and hands off to the connectors page", async ({
    accountPage: page,
    account,
  }) => {
    await page.goto("/");
    const settings = await openSettings(page, account.email);
    await settings.getByRole("button", { name: "Connectors", exact: true }).click();
    await expect(settings.getByText("No connectors yet")).toBeVisible();
    await settings.getByRole("button", { name: "Open connectors" }).click();
    // The dialog puts itself away, or the page would open inert behind it.
    await expect(settings).toBeHidden();
    await expect(page).toHaveURL(/\/connectors$/);
    await expect(page.getByRole("navigation", { name: "Breadcrumb" })).toHaveText("Connectors");
  });

  test("the help pane opens from the rail and from the account menu", async ({
    accountPage: page,
    account,
  }) => {
    await page.goto("/");
    const settings = await openSettings(page, account.email);
    await settings.getByRole("button", { name: "Get help", exact: true }).click();
    await expect(settings.getByRole("heading", { name: "Get help" })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(settings).toBeHidden();

    // With no support address configured, the menu's Get help opens the same pane.
    await accountMenu(page, account.email).click();
    await page.getByRole("menuitem", { name: "Get help" }).click();
    await expect(settings.getByRole("heading", { name: "Get help" })).toBeVisible();
  });
});
