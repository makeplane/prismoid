/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { expect, test } from "./fixtures";

// /ds is linked from nowhere and needs no account: it is the whole design system on one page.
test("the design-system page renders its sections", async ({ page }) => {
  await page.goto("/ds");
  await expect(page.getByRole("heading", { name: "Design system", level: 1 })).toBeVisible();

  const sections = page.getByRole("navigation", { name: "Sections" });
  for (const name of [
    "Map",
    "Radius",
    "Color",
    "Type",
    "Kit",
    "Propel",
    "Blocks",
    "Renderers",
    "Census",
  ]) {
    await expect(sections.getByRole("link", { name })).toBeVisible();
  }
  await sections.getByRole("link", { name: "Census" }).click();
  await expect(page).toHaveURL(/#census$/);
});
