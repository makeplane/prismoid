/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { escapeRegExp, expect, test, toast } from "../fixtures";
import { call, stamp } from "../support/api";
import { RUN_TIMEOUT, TASK_URL } from "../support/runs";

// A routine fired for real (the `models` project, E2E_MODELS=1): a routine executes nothing itself;
// its fire mints an ordinary task that runs unattended and delivers to its owner.

type Routine = { id: string; name: string };

test.describe("a routine on a real model", () => {
  // No standing work outlives the flow, pass or fail.
  test.afterEach(async ({ account }) => {
    const routines = await call<{ items: Routine[] }>("/routines", { token: account.token });
    for (const r of routines.items)
      await call(`/routines/${r.id}/retire`, { token: account.token, body: {} });
  });

  test("Run now mints a task that runs and delivers to the owner", async ({
    accountPage: page,
    account,
  }) => {
    // Once a year, so the sweep never fires it during the run: only Run now does.
    const routine = await call<Routine>("/routines", {
      token: account.token,
      body: {
        name: `Tide brief ${stamp()}`,
        instruction: "In one sentence, name the moon phase that brings spring tides.",
        schedule: { cron: "0 9 1 1 *", timezone: "UTC" },
        posture: "auto",
      },
    });
    await page.goto(`/routines?routine=${routine.id}`);
    const pane = page.getByRole("dialog", { name: "Routine" });
    await expect(
      pane.getByText("No fires yet - the ledger starts with the first one."),
    ).toBeVisible();
    await pane.getByRole("button", { name: "Run now" }).click();
    await expect(toast(page, "Fired - the run shows below.")).toBeVisible();

    // Every fire journals a run wearing its reason; a minted one opens the run its task made.
    const run = pane.getByRole("button", { name: /^minted manual · .* · task created$/ });
    await expect(run).toBeVisible();
    await run.click();
    await expect(page).toHaveURL(/[?&]run=[0-9a-f-]{36}$/);
    const banner = pane.getByRole("banner");
    await expect(banner.getByText("delivered", { exact: true })).toBeVisible({
      timeout: RUN_TIMEOUT,
    });
    await expect(pane).toContainText(/moon/i);
    await banner.getByRole("button", { name: "Open the full task" }).click();
    await expect(page).toHaveURL(TASK_URL);
    await expect(
      page.getByRole("main").getByRole("img", { name: "Status: delivered", exact: true }),
    ).toBeVisible();
    await expect(page.getByRole("region", { name: "Messages" })).toContainText(/moon/i);

    // An unattended run delivers to its owner: the inbox says so under the routine's name.
    await page.goto("/inbox");
    await expect(
      page
        .getByRole("main")
        .getByRole("button", { name: new RegExp(`${escapeRegExp(routine.name)}: delivered`) }),
    ).toBeVisible();
  });
});
