/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The flows' shared test object.
// - `seed` / `signedInPage`: the account and held task global-setup made, already signed in. For
//   flows that only read, or whose edits later flows expect (the existing smoke flows).
// - `account` / `accountPage`: a NEW account for this test alone, signed in. Every flow that changes
//   an account, asserts an empty state, or needs a clean slate takes these.
// - `adminPage`: the operator console, signed in as the seeded operator (console flows only).
// Only the sign-in flows drive the form; every other flow starts with the session already in
// localStorage, where the app keeps it.
import { test as base, expect, type Page } from "@playwright/test";
import { type Account, signUp, useSession } from "./support/accounts";
import { readSeed, type Seed } from "./seed";

export const test = base.extend<{
  seed: Seed;
  signedInPage: Page;
  account: Account;
  accountPage: Page;
  adminPage: Page;
}>({
  // Playwright reads a fixture's dependencies from its first argument: this one has none.
  seed: async ({}, use) => {
    await use(readSeed());
  },
  signedInPage: async ({ page, seed }, use) => {
    await useSession(page, seed);
    await use(page);
  },
  account: async ({}, use, testInfo) => {
    // The test's file name labels the address, so a stray account in a shared database says who made it.
    const label = `e2e-${testInfo.titlePath[0].replace(/\.spec\.ts$/, "").replace(/[^a-z0-9]+/gi, "-")}`;
    await use(await signUp(label.toLowerCase()));
  },
  accountPage: async ({ page, account }, use) => {
    await useSession(page, account);
    await use(page);
  },
  adminPage: async ({ page, seed }, use) => {
    test.skip(!seed.admin, "no operator account: the run could not reach the stack's database");
    await useSession(page, seed.admin!);
    await use(page);
  },
});

export { expect };

/** The home composer: the landing every signed-in path reaches. */
export function composer(page: Page) {
  return page.getByRole("textbox", { name: /Describe what you need done/ });
}

/** Text made safe to embed in a RegExp: accessible names are matched by pattern when they carry more. */
export function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** The account menu's trigger in the sidebar: its name is the person's initials, name and email. */
export function accountMenu(page: Page, email: string) {
  return page.getByRole("button", { name: new RegExp(escapeRegExp(email)) });
}

/**
 * A kit Section, by its heading. The section element carries no accessible name of its own, so it
 * is the one place a flow finds a region by its tag: by the heading it holds.
 */
export function section(page: Page, title: string) {
  return page
    .locator("section")
    .filter({ has: page.getByRole("heading", { name: title, exact: true }) });
}

/** Settings, opened from the account menu. */
export async function openSettings(page: Page, email: string) {
  await accountMenu(page, email).click();
  await page.getByRole("menuitem", { name: "Settings" }).click();
  return page.getByRole("dialog", { name: "Settings" });
}

/**
 * Marks a flow that a known bug breaks: the flow asserts the right behaviour and is expected to fail
 * until the bug is fixed, when Playwright reports it as an unexpected pass so the mark comes off.
 * E2E_SHOW_BUGS=1 runs these as ordinary tests, to see each failure for what it is.
 */
export function knownBug(reason: string) {
  test.info().annotations.push({ type: "bug", description: reason });
  test.fail(process.env.E2E_SHOW_BUGS !== "1", reason);
}

/** Pick one value of one composer Options setting: the row opens its choices as a submenu beside it. */
export async function chooseOption(page: Page, setting: string, value: string) {
  await page.getByRole("button", { name: "Options" }).click();
  await page
    .getByRole("menu", { name: "Options" })
    .getByRole("menuitem", { name: new RegExp(`^${escapeRegExp(setting)} `) })
    .click();
  await page
    .getByRole("menu", { name: new RegExp(`^${escapeRegExp(setting)} `) })
    .getByRole("menuitemradio", { name: new RegExp(`^${escapeRegExp(value)}`) })
    .click();
}

/** A toast's text: toasts carry no stable role, so they are found by what they say. */
export function toast(page: Page, text: string | RegExp) {
  return page.getByRole("region", { name: "Notifications" }).getByText(text);
}
