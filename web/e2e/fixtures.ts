/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The flows' shared test object. `seed` is the account and task global-setup made; a test that
// asks for `signedInPage` starts with the seed's session already in localStorage, where the app
// keeps it, so only the sign-in flow itself drives the form.
import { test as base, expect, type Page } from "@playwright/test";
import { readSeed, type Seed } from "./seed";

// The keys the app reads its session from (app/lib/storage.ts).
const TOKEN_KEY = "prismoid.token";
const EXPIRES_KEY = "prismoid.expires";

export const test = base.extend<{ seed: Seed; signedInPage: Page }>({
  // Playwright reads a fixture's dependencies from its first argument: this one has none.
  seed: async ({}, use) => {
    await use(readSeed());
  },
  signedInPage: async ({ page, seed }, use) => {
    await page.addInitScript(
      ([tokenKey, expiresKey, token, expiresAt]) => {
        localStorage.setItem(tokenKey, token);
        localStorage.setItem(expiresKey, expiresAt);
      },
      [TOKEN_KEY, EXPIRES_KEY, seed.token, seed.expiresAt] as const,
    );
    await use(page);
  },
});

export { expect };

/** The home composer: the landing every signed-in path reaches. */
export function composer(page: Page) {
  return page.getByRole("textbox", { name: /Describe what you need done/ });
}
