/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { Page } from "@playwright/test";
import { call, createHeldRoutine, type Routine, stamp } from "./support/api";
import { expect, test, toast } from "./fixtures";

// Routines (ADR-048): standing work that mints an ordinary task on every fire. Nothing here may
// start a run, so a routine is fired only when its posture is hold (an event routine made in the
// dialog, or one made through the API with posture hold): a held fire mints a task that waits.
// A schedule routine made in the dialog runs on its own, so its When is a far-off yearly cron and
// the flow retires it before it ends.

const FAR_CRON = "0 9 1 1 *";

/** The routine's detail peek. Toasts are dialogs too, hence the exact name. */
function peekOf(page: Page) {
  return page.getByRole("dialog", { name: "Routine", exact: true });
}

/** The list's row for a routine: its name, its When, then its status last. */
function listRow(page: Page, name: string) {
  return page.getByRole("main").getByRole("button", { name: new RegExp(`^${name} `) });
}

async function openNewRoutine(page: Page) {
  await page.getByRole("button", { name: "New routine" }).click();
  return page.getByRole("dialog", { name: "New routine" });
}

/** Run now on a held routine (the only kind a flow may fire), once its peek is open. */
async function runNow(page: Page) {
  await peekOf(page).getByRole("button", { name: "Run now" }).click();
}

test("the New routine dialog needs a name, an instruction and a When", async ({
  accountPage: page,
}) => {
  await page.goto("/routines");
  await expect(page.getByRole("main").getByText("No routines yet")).toBeVisible();

  const dialog = await openNewRoutine(page);
  const save = dialog.getByRole("button", { name: "Save routine" });
  await expect(dialog.getByText("Pick a When - a routine needs one.")).toBeVisible();
  await expect(save).toBeDisabled();
  await dialog.getByRole("textbox", { name: "Name" }).fill(`Briefing ${stamp()}`);
  await expect(save).toBeDisabled();
  await dialog.getByRole("textbox", { name: "Instruction" }).fill("Summarize the week's tasks.");
  await expect(save).toBeDisabled();

  // Every way to say When reads back as one line beside Save.
  for (const [preset, line] of [
    ["Every weekday, 9:00", "cron 0 9 * * 1-5"],
    ["Every day, 9:00", "cron 0 9 * * *"],
    ["Every hour", "cron 0 * * * *"],
    ["Mondays, 9:00", "cron 0 9 * * 1"],
  ] as const) {
    await dialog.getByRole("button", { name: preset }).click();
    await expect(dialog.getByText(line, { exact: true })).toBeVisible();
    await expect(save).toBeEnabled();
  }
  await dialog.getByPlaceholder(/^cron, e\.g\./).fill("30 8 * * 1-5");
  await expect(dialog.getByText("cron 30 8 * * 1-5", { exact: true })).toBeVisible();
  await dialog.getByPlaceholder(/^every, e\.g\./).fill("4h");
  await expect(dialog.getByText("every 4h", { exact: true })).toBeVisible();
  await expect(dialog.getByPlaceholder(/^cron, e\.g\./)).toHaveValue("");

  // The events lane replaces the schedule; event routines say they start held.
  await dialog.getByRole("button", { name: "a task delivers" }).click();
  await expect(dialog.getByText("when task.delivered", { exact: true })).toBeVisible();
  await expect(dialog.getByText(/Event routines start held/)).toBeVisible();
  await expect(dialog.getByPlaceholder(/^every, e\.g\./)).toHaveValue("");
  await dialog.getByRole("button", { name: "a task fails" }).click();
  await expect(
    dialog.getByText("when task.delivered or task.failed", { exact: true }),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "a task delivers" }).click();
  await dialog.getByRole("button", { name: "a task fails" }).click();
  await expect(dialog.getByText("Pick a When - a routine needs one.")).toBeVisible();
  await expect(save).toBeDisabled();

  // Closing saves nothing.
  await dialog.getByRole("button", { name: "Close" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByRole("main").getByText("No routines yet")).toBeVisible();
});

test("refuses a cron the server cannot read, with its reason", async ({ accountPage: page }) => {
  await page.goto("/routines");
  const dialog = await openNewRoutine(page);
  await dialog.getByRole("textbox", { name: "Name" }).fill(`Broken clock ${stamp()}`);
  await dialog.getByRole("textbox", { name: "Instruction" }).fill("Never mind.");
  await dialog.getByPlaceholder(/^cron, e\.g\./).fill("99 99 * * *");
  await dialog.getByRole("button", { name: "Save routine" }).click();
  await expect(toast(page, "cron rejected: Bad minute")).toBeVisible();
  // The dialog stays with the draft, and nothing was saved.
  await expect(dialog).toBeVisible();
  await expect(dialog.getByPlaceholder(/^cron, e\.g\./)).toHaveValue("99 99 * * *");
  await dialog.getByRole("button", { name: "Close" }).click();
  await expect(page.getByRole("main").getByText("No routines yet")).toBeVisible();
});

test("saves a schedule routine, pauses, resumes and retires it", async ({ accountPage: page }) => {
  const name = `Yearly review ${stamp()}`;
  await page.goto("/routines");
  const dialog = await openNewRoutine(page);
  await dialog.getByRole("textbox", { name: "Name" }).fill(name);
  await dialog.getByRole("textbox", { name: "Instruction" }).fill("Review the year's projects.");
  await dialog.getByPlaceholder(/^cron, e\.g\./).fill(FAR_CRON);
  await dialog.getByRole("textbox", { name: "Budget per run" }).fill("1.50");
  await dialog.getByRole("button", { name: "Save routine" }).click();
  await expect(toast(page, /^Routine saved - cron 0 9 1 1 \*, first fire /)).toBeVisible();

  // Saving opens its peek, deep-linked by id.
  const peek = peekOf(page);
  await expect(peek.getByRole("heading", { level: 2, name })).toBeVisible();
  await expect(page).toHaveURL(/\/routines\?routine=[0-9a-f-]{36}$/);
  const url = page.url();
  await expect(peek.getByText(`When: cron ${FAR_CRON}`)).toBeVisible();
  await expect(peek.getByText("$1.50 / run")).toBeVisible();
  await expect(
    peek.getByText("No fires yet - the ledger starts with the first one."),
  ).toBeVisible();

  await page.reload();
  await expect(page).toHaveURL(url);
  await expect(peek.getByRole("heading", { level: 2, name })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(peek).toBeHidden();
  await expect(page).toHaveURL(/\/routines$/);
  await expect(listRow(page, name)).toHaveAccessibleName(new RegExp(`cron 0 9 1 1 \\* .*active$`));

  await listRow(page, name).click();
  await peek.getByRole("button", { name: "Pause" }).click();
  await expect(toast(page, "Paused.")).toBeVisible();
  await expect(peek.getByRole("button", { name: "Resume" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(listRow(page, name)).toHaveAccessibleName(/paused$/);

  await listRow(page, name).click();
  await peek.getByRole("button", { name: "Resume" }).click();
  await expect(toast(page, "Resumed - next fire recomputed.")).toBeVisible();
  await expect(peek.getByRole("button", { name: "Pause" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(listRow(page, name)).toHaveAccessibleName(/active$/);

  // Retiring closes the peek and keeps the row, retired, with no way to fire it again.
  await listRow(page, name).click();
  await peek.getByRole("button", { name: "More" }).click();
  await page.getByRole("menuitem", { name: "Retire routine" }).click();
  await expect(toast(page, "Retired - the run history stays.")).toBeVisible();
  await expect(peek).toBeHidden();
  await expect(listRow(page, name)).toHaveAccessibleName(/retired$/);
  await page.reload();
  await listRow(page, name).click();
  await expect(peek.getByRole("heading", { level: 2, name })).toBeVisible();
  await expect(peek.getByRole("button", { name: "Run now" })).toHaveCount(0);
  await expect(peek.getByRole("button", { name: "Pause" })).toHaveCount(0);
});

test("an event routine is held, and Run now mints a held run that opens in the sheet", async ({
  accountPage: page,
}) => {
  const name = `Delivery digest ${stamp()}`;
  await page.goto("/routines");
  const dialog = await openNewRoutine(page);
  await dialog.getByRole("textbox", { name: "Name" }).fill(name);
  await dialog.getByRole("textbox", { name: "Instruction" }).fill("Digest what was delivered.");
  await dialog.getByRole("button", { name: "a task delivers" }).click();
  await dialog.getByRole("button", { name: "Save routine" }).click();
  await expect(toast(page, "Routine saved - when task.delivered.")).toBeVisible();

  const peek = peekOf(page);
  await expect(peek.getByRole("heading", { level: 2, name })).toBeVisible();
  // Held is what makes Run now safe here: the fire mints a task that waits for a person.
  await expect(
    peek.getByText("When: when task.delivered · runs wait for your release"),
  ).toBeVisible();

  await runNow(page);
  await expect(toast(page, "Fired - the task waits below until you release it.")).toBeVisible();
  const run = peek.getByRole("button", { name: /^held manual · / });
  await expect(run).toHaveCount(1);

  // A run opens its task in the same sheet, with the way back.
  await run.click();
  await expect(page).toHaveURL(/[?&]run=[0-9a-f-]{36}/);
  await expect(peek.getByText(`${name} · run`)).toBeVisible();
  await expect(peek.getByText("Digest what was delivered.")).toBeVisible();
  await peek.getByRole("button", { name: "Back to the routine" }).click();
  await expect(page).not.toHaveURL(/run=/);
  await expect(peek.getByRole("heading", { level: 2, name })).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(listRow(page, name)).toHaveAccessibleName(/when task\.delivered active$/);
});

test("a fire token fires a held routine until it is revoked", async ({
  accountPage: page,
  account,
}) => {
  const routine: Routine = await createHeldRoutine(account.token, {
    name: `Webhook ${stamp()}`,
    instruction: "Note the webhook's arrival.",
  });
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto(`/routines?routine=${routine.id}`);
  const peek = peekOf(page);
  await expect(peek.getByRole("heading", { level: 2, name: routine.name })).toBeVisible();

  await peek.getByRole("button", { name: "More" }).click();
  await page.getByRole("menuitem", { name: "Mint fire token…" }).click();
  await expect(toast(page, /^Fire token copied\. Shown once/)).toBeVisible();
  const token = await page.evaluate(() => navigator.clipboard.readText());
  expect(token).toMatch(/^rt_/);

  // The token alone opens the routine's fire door.
  const fired = await call<{ decision: string }>(`/routines/${routine.id}/fire`, {
    token,
    body: {},
  });
  expect(fired.decision).toBe("held");
  await page.reload();
  await expect(peek.getByRole("button", { name: /^held api · / })).toBeVisible();

  await peek.getByRole("button", { name: "More" }).click();
  await page.getByRole("menuitem", { name: "Revoke fire token" }).click();
  await expect(toast(page, "Fire door closed.")).toBeVisible();
  await expect(call(`/routines/${routine.id}/fire`, { token, body: {} })).rejects.toMatchObject({
    status: 401,
  });
});

test("Run now on a routine whose season is over says the fire was skipped", async ({
  accountPage: page,
  account,
}) => {
  // A held routine allowed one run, which it has already had: the next fire is skipped.
  const routine = await call<Routine>("/routines", {
    token: account.token,
    body: {
      name: `One season ${stamp()}`,
      instruction: "Only once.",
      schedule: { cron: FAR_CRON, timezone: "UTC" },
      posture: "hold",
      max_runs: 1,
    },
  });
  await call(`/routines/${routine.id}/fire`, { token: account.token, body: {} });
  await page.goto(`/routines?routine=${routine.id}`);
  const peek = peekOf(page);
  await expect(peek.getByRole("button", { name: /^held manual · / })).toBeVisible();

  await runNow(page);
  // The answer lands first: the routine retires, so Run now goes away.
  await expect(peek.getByRole("button", { name: "Run now" })).toHaveCount(0);
  await expect(toast(page, "Not fired (skipped season) - the run shows below.")).toBeVisible();
  // A toast that has already shown cannot be waited away, so this reads it once, no retry.
  expect(await toast(page, "Fired - the run shows below.").count()).toBe(0);
});
