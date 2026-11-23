/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { Page } from "@playwright/test";
import { chooseOption, composer, escapeRegExp, expect, knownBug, test, toast } from "./fixtures";
import { call, createBot, createHeldTask, stamp } from "./support/api";

// The home composer. Sending with When set to Now starts a task that calls a model, so these flows
// never press Submit or Enter with a goal and When = Now: they set dials and read them back, and the
// only submits carry a schedule, which saves a routine instead of starting anything.

type RoutineRow = { id: string; name: string; posture: string; trigger_kind: string };
type Listing<T> = { items: T[] };

/**
 * A yearly schedule typed into the When panel, set while the box is still empty so no keypress in
 * the panel can ever send a goal. Once a year keeps the sweep from firing the routine during a run.
 */
async function scheduleYearly(page: Page) {
  await page.getByRole("button", { name: "Options" }).click();
  await page.getByRole("menuitem", { name: /^When / }).click();
  await page.getByRole("menuitem", { name: /^Custom schedule or event/ }).click();
  const cron = page.getByRole("textbox", { name: /cron/ });
  await cron.fill("0 9 1 1 *");
  await cron.press("Enter");
  await expect(page.getByRole("button", { name: "Clear schedule" })).toBeVisible();
}

/** Attach a small text file through the composer's own file chooser. */
async function attachNotes(page: Page) {
  await page.getByRole("button", { name: "Add files or capture" }).click();
  const chooser = page.waitForEvent("filechooser");
  await page.getByRole("menuitem", { name: "Attach files" }).click();
  await (
    await chooser
  ).setFiles({ name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("hello") });
}

/** Submit a goal on a yearly schedule together with something a routine cannot carry. */
async function submitRefused(page: Page, goal: string, extra: "fleet" | "attachment") {
  await scheduleYearly(page);
  if (extra === "fleet") await chooseOption(page, "Mode", "Fleet");
  else await attachNotes(page);
  await composer(page).fill(goal);
  await expect(composer(page)).toHaveValue(goal);
  await expect(page.getByRole("button", { name: "Clear schedule" })).toBeVisible();
  await page.getByRole("button", { name: "Submit" }).click();
}

const FLEET_REFUSAL = "Routines run standard tasks. Switch the mode dial off fleet first.";
const ATTACHMENT_REFUSAL = /^Routines can't carry attachments yet/;

test.describe("home composer", () => {
  test("a fresh account lands on an empty composer with nothing recent", async ({
    accountPage: page,
  }) => {
    const listed = page.waitForResponse(
      (r) => r.url().includes("/v1/tasks?pinned=false") && r.request().method() === "GET",
    );
    await page.goto("/");
    await listed;
    // The greeting rotates by time of day, so only its level is stable.
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    const submit = page.getByRole("button", { name: "Submit" });
    await expect(composer(page)).toBeFocused();
    await expect(submit).toBeDisabled();
    await composer(page).fill("Outline a reading list");
    await expect(submit).toBeEnabled();
    await composer(page).fill("");
    await expect(submit).toBeDisabled();
    // An account with no tasks keeps the landing pure: no Recent section and no show/hide chrome.
    await expect(page.getByRole("heading", { name: "Recent" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: /Show recent|Hide/ })).toHaveCount(0);
  });

  test("Options settings show as pills that clear back to the defaults", async ({
    accountPage: page,
  }) => {
    await page.goto("/");
    const box = page.getByRole("group").filter({ has: composer(page) });

    await chooseOption(page, "Size", "Brief");
    await expect(box.getByRole("combobox", { name: "Size", exact: true })).toHaveText(/Brief/);
    await chooseOption(page, "Deliverable", "Document · PDF");
    await expect(box.getByRole("button", { name: "Deliverable", exact: true })).toHaveText(
      /Document · PDF/,
    );
    await chooseOption(page, "Clarifying questions", "Never");
    await expect(box.getByRole("combobox", { name: "Clarifying questions" })).toHaveText(/never/i);
    await chooseOption(page, "Budget", "$5");
    await expect(box.getByRole("button", { name: /^Budget/ })).toHaveText(/\$5\.00/);
    await chooseOption(page, "When", "Every day, 9:00");
    await expect(box.getByRole("button", { name: "When this runs" })).toBeVisible();
    await chooseOption(page, "Mode", "Fleet");
    await expect(box.getByRole("combobox", { name: "Mode", exact: true })).toHaveText(/Fleet/);
    // A fleet delivers the chair's table, so the deliverable dial steps aside while it is on.
    await expect(box.getByRole("button", { name: "Clear deliverable" })).toHaveCount(0);
    await box.getByRole("button", { name: "Clear fleet mode" }).click();
    await expect(box.getByRole("button", { name: "Clear deliverable" })).toBeVisible();

    for (const name of ["schedule", "size", "deliverable", "clarifying questions", "budget"]) {
      await box.getByRole("button", { name: `Clear ${name}` }).click();
      await expect(box.getByRole("button", { name: `Clear ${name}` })).toHaveCount(0);
    }
    await page.getByRole("button", { name: "Options" }).click();
    const options = page.getByRole("menu", { name: "Options" });
    for (const row of [
      "When Now",
      "Mode Standard",
      "Size Auto",
      "Deliverable Auto",
      "Clarifying questions Auto",
      "Budget No cap",
    ])
      await expect(options.getByRole("menuitem", { name: row, exact: true })).toBeVisible();
  });

  test("a custom budget takes a typed amount", async ({ accountPage: page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Options" }).click();
    await page.getByRole("menuitem", { name: /^Budget / }).click();
    await page.getByRole("menuitem", { name: "Custom amount…" }).click();
    const amount = page.getByRole("textbox", { name: "Spend cap in USD" });
    await amount.fill("3.5");
    await amount.press("Enter");
    const pill = page.getByRole("button", { name: /^Budget/ });
    await expect(pill).toHaveText(/\$3\.50/);
    await page.getByRole("button", { name: "Clear budget" }).click();
    await expect(page.getByRole("button", { name: "Clear budget" })).toHaveCount(0);
  });

  test("the project picker creates a project inline and remembers it", async ({
    accountPage: page,
    account,
  }) => {
    const name = `Desk ${stamp()}`;
    await page.goto("/");
    const picker = page.getByRole("combobox", { name: "Project: where this task will live" });
    await picker.click();
    const popup = page.getByRole("dialog", { name: "Project" });
    await expect(popup.getByRole("option", { name: "No project" })).toBeVisible();
    await popup.getByRole("combobox", { name: "Search or create a project…" }).fill(name);
    await popup.getByRole("option", { name: `Create “${name}”` }).click();

    const chosen = page.getByRole("combobox", { name: `Project: ${name}` });
    await expect(chosen).toBeVisible();
    const projects = await call<Listing<{ name: string }>>("/projects", { token: account.token });
    expect(projects.items.map((p) => p.name)).toContain(name);

    // The last destination is the default next time, and "No project" puts the task back home.
    await page.reload();
    await expect(chosen).toBeVisible();
    await chosen.click();
    await page
      .getByRole("dialog", { name: "Project" })
      .getByRole("option", { name: "No project" })
      .click();
    await expect(picker).toBeVisible();
  });

  test("Skills, Bot and Connectors rows pin, clear and link out", async ({
    accountPage: page,
    account,
  }) => {
    // A draft bot is enough for the Bot row to offer it.
    const bot = await createBot(account.token, { name: `scout-${stamp()}` });
    await page.goto("/");
    const box = page.getByRole("group").filter({ has: composer(page) });

    await page.getByRole("button", { name: "Options" }).click();
    await page.getByRole("menuitem", { name: /^Skills / }).click();
    await page.getByRole("menuitemcheckbox", { name: "summarize", exact: true }).click();
    await expect(page.getByRole("menuitem", { name: "Skills 1 pinned" })).toBeVisible();
    await page.keyboard.press("Escape");
    await page.keyboard.press("Escape");
    await expect(box.getByRole("button", { name: "Skills", exact: true })).toHaveText(/1/);
    await box.getByRole("button", { name: "Clear skills" }).click();
    await expect(box.getByRole("button", { name: "Clear skills" })).toHaveCount(0);

    await chooseOption(page, "Bot", `${bot.name} (draft)`);
    await expect(box.getByRole("button", { name: `Bot: ${bot.name}` })).toBeVisible();
    await box.getByRole("button", { name: "Clear bot" }).click();
    await expect(box.getByRole("button", { name: `Bot: ${bot.name}` })).toHaveCount(0);

    // A fresh account has no connector to attach, so the row points at where one is added.
    await page.getByRole("button", { name: "Options" }).click();
    await page.getByRole("menuitem", { name: "Connectors None" }).click();
    await page.getByRole("menuitem", { name: "Add connector…" }).click();
    await expect(page).toHaveURL(/\/connectors$/);
  });

  test("attaching a file shows a chip that removes it", async ({ accountPage: page }) => {
    await page.goto("/");
    const submit = page.getByRole("button", { name: "Submit" });
    await attachNotes(page);
    const box = page.getByRole("group").filter({ has: composer(page) });
    await expect(box.getByText("notes.txt")).toBeVisible();
    await box.getByRole("button", { name: "Remove" }).click();
    await expect(box.getByText("notes.txt")).toHaveCount(0);
    await expect(submit).toBeDisabled();
  });

  test("Recent lists the account's tasks and hides on request", async ({
    accountPage: page,
    account,
  }) => {
    const task = await createHeldTask(account.token, "Sort the field notes");
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Recent" })).toBeVisible();
    const main = page.getByRole("main");
    await expect(
      main.getByRole("link", { name: new RegExp(escapeRegExp(task.title)) }),
    ).toBeVisible();

    const hide = main.getByRole("button", { name: "Hide" });
    await expect(hide).toHaveAttribute("aria-expanded", "true");
    await hide.click();
    const show = main.getByRole("button", { name: "Show recent" });
    await expect(show).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByRole("heading", { name: "Recent" })).toHaveCount(0);
    // Hiding is a preference: it survives a reload.
    await page.reload();
    await expect(show).toBeVisible();
    await show.click();
    await expect(page.getByRole("heading", { name: "Recent" })).toBeVisible();

    await main.getByRole("link", { name: "View all" }).click();
    await expect(page).toHaveURL(/\/tasks$/);
  });
});

// Every routine a flow saves is retired after it, pass or fail, so no standing work outlives a run.
test.describe("a scheduled ask saves a routine", () => {
  test.afterEach(async ({ account }) => {
    const routines = await call<Listing<RoutineRow>>("/routines", { token: account.token });
    for (const r of routines.items)
      await call(`/routines/${r.id}/retire`, { token: account.token, body: {} });
  });

  test("submitting with a schedule saves a routine and starts no task", async ({
    accountPage: page,
    account,
  }) => {
    const goal = `Summarize the week's notes ${stamp()}`;
    await page.goto("/");
    await scheduleYearly(page);
    await expect(page.getByRole("button", { name: "When this runs" })).toHaveText(/0 9 1 1 \*/);
    await composer(page).fill(goal);
    await expect(page.getByRole("button", { name: "Clear schedule" })).toBeVisible();
    await page.getByRole("button", { name: "Submit" }).click();

    // The first fire is stated in the browser's time zone, so only the start of the toast is fixed.
    await expect(toast(page, /^Routine saved - /).first()).toBeVisible();
    await expect(page).toHaveURL(/\/routines\?routine=[0-9a-f-]{36}$/);
    const id = new URL(page.url()).searchParams.get("routine");
    const pane = page.getByRole("dialog", { name: "Routine" });
    await expect(pane.getByRole("heading", { name: goal })).toBeVisible();

    const routines = await call<Listing<RoutineRow>>("/routines", { token: account.token });
    expect(routines.items).toHaveLength(1);
    expect(routines.items[0]).toMatchObject({ id, name: goal, trigger_kind: "schedule" });
    test.info().annotations.push({ type: "posture", description: routines.items[0].posture });
    // Saved, not sent: the account still has no task at all.
    const tasks = await call<Listing<unknown>>("/tasks", { token: account.token });
    expect(tasks.items).toHaveLength(0);

    await page.goto("/routines");
    await expect(
      page.getByRole("main").getByRole("button", { name: new RegExp(escapeRegExp(goal)) }),
    ).toBeVisible();
  });

  test("a schedule refuses fleet mode and attachments and saves nothing", async ({
    accountPage: page,
    account,
  }) => {
    await page.goto("/");
    await submitRefused(page, "Compare three vendors", "fleet");
    await expect(toast(page, FLEET_REFUSAL)).toBeVisible();
    await expect(page).toHaveURL(/\/$/);

    await page.goto("/");
    await submitRefused(page, "Read the attached notes", "attachment");
    await expect(toast(page, ATTACHMENT_REFUSAL)).toBeVisible();
    await expect(page).toHaveURL(/\/$/);
    const routines = await call<Listing<RoutineRow>>("/routines", { token: account.token });
    expect(routines.items).toHaveLength(0);
  });

  test("a refused fleet schedule keeps the typed goal", async ({ accountPage: page }) => {
    knownBug(
      "web/app/components/composer/task-composer.tsx:232-235 - the fleet refusal returns instead " +
        "of throwing, so composer.tsx:270-272 treats the save as sent and clears the typed goal",
    );
    const goal = "Compare three vendors";
    await page.goto("/");
    await submitRefused(page, goal, "fleet");
    await expect(toast(page, FLEET_REFUSAL)).toBeVisible();
    await expect(composer(page)).toHaveValue(goal);
  });

  test("a refused attachment schedule keeps the goal and the file", async ({
    accountPage: page,
  }) => {
    knownBug(
      "web/app/components/composer/task-composer.tsx:237-240 - the attachments refusal returns " +
        "instead of throwing, so composer.tsx:270-273 clears the typed goal and the attached file",
    );
    const goal = "Read the attached notes";
    await page.goto("/");
    await submitRefused(page, goal, "attachment");
    await expect(toast(page, ATTACHMENT_REFUSAL)).toBeVisible();
    const box = page.getByRole("group").filter({ has: composer(page) });
    await expect(box.getByText("notes.txt")).toBeVisible();
    await expect(composer(page)).toHaveValue(goal);
  });
});
