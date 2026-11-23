/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { Page } from "@playwright/test";
import { signUp } from "../support/accounts";
import { call, createBot, createHeldRoutine, stamp } from "../support/api";
import { escapeRegExp, expect, test, toast } from "../fixtures";

// Bots and routines across the system. A bot is observed read-only, and reading its instructions is
// itself an audited act. A routine has one operator verb, pause, which needs a reason and tells the
// owner. The routines here hold (posture "hold", a yearly When), so nothing fires during a run.

test.describe("console bots", () => {
  test("finds a fresh draft bot and reading its instructions is audited", async ({
    adminPage: page,
  }) => {
    const owner = await signUp("e2e-admin-bots");
    const name = `heron-${stamp()}`;
    const instructions = "Summarize the field notes in three short bullet points.";
    await createBot(owner.token, { name, instructions });

    await page.goto("/bots");
    await page.getByRole("textbox", { name: "Search bot name…" }).fill(name);
    const main = page.getByRole("main");
    const row = main.getByRole("link", { name: new RegExp(`^${escapeRegExp(name)} `) });
    await expect(row).toHaveCount(1);
    // Every bot row carries its eval state; once the search has landed this is the only row.
    await expect(main.getByRole("link", { name: / evals / })).toHaveCount(1);
    for (const fact of ["v1", "draft", "evals untested", owner.email, "personal"])
      await expect(row).toContainText(fact);

    // The status chips: a draft is a draft, and nothing else.
    await main.getByRole("button", { name: "Draft", exact: true }).click();
    await expect(page).toHaveURL(/status=draft/);
    await expect(row).toHaveCount(1);
    for (const other of ["Published", "Archived"]) {
      await main.getByRole("button", { name: other, exact: true }).click();
      await expect(main.getByText(`No matches for “${name}”`)).toBeVisible();
    }
    await main.getByRole("button", { name: "All", exact: true }).click();
    await expect(row).toHaveCount(1);

    await row.click();
    await expect(page).toHaveURL(/\/bots\/[0-9a-f-]{36}$/);
    const botId = new URL(page.url()).pathname.split("/").pop()!;
    await expect(main.getByRole("heading", { name, level: 1 })).toBeVisible();
    await expect(main.getByRole("heading", { name: "Instructions", level: 2 })).toBeVisible();
    await expect(main.getByText(instructions)).toBeVisible();
    await expect(
      main.getByText("Reading a bot's instructions is recorded in the audit trail."),
    ).toBeVisible();

    await main.getByRole("tab", { name: "Versions" }).click();
    await expect(main.getByRole("tab", { name: "Versions", selected: true })).toBeVisible();
    const version = main.getByRole("listitem").filter({ hasText: "v1" });
    await expect(version).toContainText("head");
    await expect(version).toContainText("draft");
    await expect(main.getByText(instructions)).toBeHidden();

    // The read left a row on the audit trail, naming the bot.
    await page.goto("/audit");
    const entry = page
      .getByRole("main")
      .getByRole("listitem")
      .filter({ hasText: "bot.view" })
      .filter({ has: page.getByRole("link", { name: `bot:${botId}` }) });
    await expect(entry.first()).toBeVisible();
    await entry
      .first()
      .getByRole("link", { name: `bot:${botId}` })
      .click();
    await expect(page).toHaveURL(new RegExp(`/bots/${botId}$`));
  });
});

/**
 * A fresh person's held routine, found on the console's list by its unique name: typed into the
 * search box, or opened from a link that already carries the search.
 */
async function routineOnList(page: Page, via: "typed" | "link") {
  const owner = await signUp("e2e-admin-routines");
  const name = `Tide report ${stamp()}`;
  const routine = await createHeldRoutine(owner.token, {
    name,
    instruction: "Summarize the tide tables.",
  });
  const search = page.getByRole("textbox", { name: "Search routine names…" });
  if (via === "typed") {
    await page.goto("/routines");
    await search.fill(name);
  } else {
    await page.goto(`/routines?q=${encodeURIComponent(name)}`);
    await expect(search).toHaveValue(name);
  }
  // Every routine row counts its runs. Once the search has landed (the box debounces), this is
  // the only such row, so nothing after this races a refetch.
  const rows = page
    .getByRole("main")
    .getByRole("listitem")
    .filter({ hasText: /\d+ runs?/ });
  const row = rows.filter({ hasText: name });
  await expect(row).toHaveCount(1);
  await expect(rows.filter({ hasNotText: name })).toHaveCount(0);
  return { owner, name, routine, row };
}

/** Opens the pause dialog from the row's nested pause button. */
async function openPause(page: Page, name: string) {
  await page.getByRole("button", { name: `Pause ${name}`, exact: true }).click();
  return page.getByRole("dialog", { name: `Pause “${name}”?` });
}

test.describe("console routines", () => {
  test("an operator pauses a routine with a reason, and the owner is told", async ({
    adminPage: page,
  }) => {
    const { owner, name, routine, row } = await routineOnList(page, "typed");
    await expect(row).toContainText(owner.email);
    await expect(row).toContainText("active");

    const dialog = await openPause(page, name);
    const confirm = dialog.getByRole("button", { name: "Pause routine" });
    await expect(confirm).toBeDisabled();
    const reason = `Runaway schedule draining the budget ${stamp()}`;
    await dialog.getByRole("textbox", { name: "Reason" }).fill(reason);
    await confirm.click();
    await expect(toast(page, "Paused. Audited, and the owner has the note.")).toBeVisible();
    await expect(dialog).toBeHidden();

    // The owner's routine is paused, carrying the operator's note.
    const seen = await call<{ status: string; paused_reason: string; pause_note: string }>(
      `/routines/${routine.id}`,
      { token: owner.token },
    );
    expect(seen).toMatchObject({ status: "paused", paused_reason: "admin", pause_note: reason });
    const inbox = await call<{ items: { title: string; body: string }[] }>("/inbox", {
      token: owner.token,
    });
    const note = inbox.items.find((i) => i.title === `${name} was paused by an operator`);
    expect(note?.body).toContain(reason);

    // Read back from the server, the row says paused, with the note, and offers no second pause.
    await page.reload();
    await expect(row).toContainText("paused");
    await expect(row).toContainText(`admin: ${reason}`);
    await expect(page.getByRole("button", { name: `Pause ${name}`, exact: true })).toHaveCount(0);
    const main = page.getByRole("main");
    await main.getByRole("button", { name: "Paused", exact: true }).click();
    await expect(page).toHaveURL(/status=paused/);
    await expect(row).toHaveCount(1);
    await main.getByRole("button", { name: "Active", exact: true }).click();
    await expect(main.getByText(`No matches for “${name}”`)).toBeVisible();
  });

  test("the pause confirm waits for a reason the server accepts", async ({ adminPage: page }) => {
    const { name } = await routineOnList(page, "link");
    const dialog = await openPause(page, name);
    // The door takes a reason of three characters or more: a shorter one is held back here.
    const reason = dialog.getByRole("textbox", { name: "Reason" });
    const confirm = dialog.getByRole("button", { name: "Pause routine" });
    await expect(dialog.getByText("At least 3 characters.")).toBeVisible();
    await reason.fill("ok");
    await expect(confirm).toBeDisabled();
    await reason.fill("ok!");
    await confirm.click();
    await expect(toast(page, "Paused. Audited, and the owner has the note.")).toBeVisible();
  });

  test("a paused routine shows as paused without a reload", async ({ adminPage: page }) => {
    const { name, row } = await routineOnList(page, "link");
    const dialog = await openPause(page, name);
    await dialog.getByRole("textbox", { name: "Reason" }).fill("Paused while the owner is away");
    await dialog.getByRole("button", { name: "Pause routine" }).click();
    await expect(toast(page, "Paused. Audited, and the owner has the note.")).toBeVisible();
    await expect(row).toContainText("paused");
  });
});
