/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { Page } from "@playwright/test";
import { joinWorkspace, pageFor, signUp, verifyEmail } from "./support/accounts";
import { createProject, stamp } from "./support/api";
import { mailAvailable } from "./support/mailbox";
import { expect, knownBug, section, test, toast } from "./fixtures";

// A project's Settings tab, as its owner sees it. Each setting saves on its own Save, which
// appears only while that field differs from the server, so several can be on screen at once.

/** A kit Section is a bare <section> under an h2: found by its heading, the only name it has. */

/** The settings row holding this textbox: its Save sits beside it. */
function rowOf(page: Page, textbox: string) {
  return page
    .getByRole("tabpanel", { name: "Settings" })
    .getByRole("listitem")
    .filter({ has: page.getByRole("textbox", { name: textbox }) });
}

async function openSettings(page: Page, projectId: string) {
  await page.goto(`/projects/${projectId}?tab=settings`);
  await expect(page.getByRole("tab", { name: "Settings" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
}

test("renames the project, and the new name is the one everywhere", async ({
  accountPage: page,
  account,
}) => {
  const project = await createProject(account.token, `Kelp census ${stamp()}`);
  await openSettings(page, project.id);
  const row = rowOf(page, "Project name");
  const name = row.getByRole("textbox", { name: "Project name" });
  await expect(name).toHaveValue(project.name);
  // A resting field offers nothing to save.
  await expect(row.getByRole("button", { name: "Save" })).toHaveCount(0);

  const renamed = `Kelp forest census ${stamp()}`;
  await name.fill(renamed);
  await row.getByRole("button", { name: "Save" }).click();
  await expect(toast(page, "Saved.")).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(renamed);
  await expect(row.getByRole("button", { name: "Save" })).toHaveCount(0);

  await page.reload();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(renamed);
  await expect(name).toHaveValue(renamed);
  await page.goto("/projects");
  await expect(
    page.getByRole("main").getByRole("link", { name: new RegExp(renamed) }),
  ).toBeVisible();
});

test("saves the project's instructions", async ({ accountPage: page, account }) => {
  const project = await createProject(account.token, `Reef notes ${stamp()}`);
  await openSettings(page, project.id);
  const instructions = section(page, "Instructions");
  const text = instructions.getByRole("textbox", { name: "Project instructions" });
  await expect(instructions.getByRole("button", { name: "Save" })).toHaveCount(0);

  await text.fill("Cite sources.\nUK English.");
  await instructions.getByRole("button", { name: "Save" }).click();
  await expect(toast(page, "Saved.")).toBeVisible();
  await expect(instructions.getByRole("button", { name: "Save" })).toHaveCount(0);

  await page.reload();
  await expect(text).toHaveValue("Cite sources.\nUK English.");
});

test("sets a monthly budget, then clears it", async ({ accountPage: page, account }) => {
  const project = await createProject(account.token, `Budgeted ${stamp()}`);
  await openSettings(page, project.id);
  const budget = section(page, "Budget");
  const row = rowOf(page, "Budget in USD");
  const input = row.getByRole("textbox", { name: "Budget in USD" });
  await expect(budget.getByText("spent this month · no ceiling")).toBeVisible();

  await input.fill("25");
  await row.getByRole("button", { name: "Save" }).click();
  await expect(toast(page, "Saved.")).toBeVisible();
  await expect(budget.getByText(/of \$25\.00 this month/)).toBeVisible();
  await expect(row.getByRole("button", { name: "Save" })).toHaveCount(0);

  await page.reload();
  await expect(budget.getByText(/of \$25\.00 this month/)).toBeVisible();
  // The server writes the ceiling with its own precision; it is the same 25.
  expect(Number(await input.inputValue())).toBe(25);

  // An empty field clears the ceiling rather than saving zero.
  await input.fill("");
  await row.getByRole("button", { name: "Save" }).click();
  await expect(budget.getByText("spent this month · no ceiling")).toBeVisible();
  await page.reload();
  await expect(input).toHaveValue("");
  await expect(budget.getByText("spent this month · no ceiling")).toBeVisible();
});

test("adds a project memory that says who wrote it", async ({ accountPage: page, account }) => {
  const project = await createProject(account.token, `Remembered ${stamp()}`);
  await openSettings(page, project.id);
  const memory = section(page, "Project memory");
  const add = memory.getByRole("button", { name: "Add" });
  await expect(add).toBeDisabled();

  const note = `The tide tables live in the shared drive ${stamp()}.`;
  await memory.getByRole("textbox", { name: "Add a project memory" }).fill(note);
  await add.click();
  const row = memory.getByRole("listitem").filter({ hasText: note });
  await expect(row).toBeVisible();
  await expect(row.getByText(/added by you/)).toBeVisible();
  await expect(memory.getByRole("textbox", { name: "Add a project memory" })).toHaveValue("");

  await page.reload();
  await expect(memory.getByRole("listitem").filter({ hasText: note })).toBeVisible();
});

test("each Save keeps the other fields' unsaved edits", async ({ accountPage: page, account }) => {
  knownBug(
    "web/app/routes/projects.$id/route.tsx:576-577 - SettingsTab.save() echoes BOTH name and " +
      "budget from the server after any save, so saving the name throws away an unsaved budget edit",
  );
  const project = await createProject(account.token, `Two edits ${stamp()}`);
  await openSettings(page, project.id);
  const nameRow = rowOf(page, "Project name");
  const budgetRow = rowOf(page, "Budget in USD");
  const instructions = section(page, "Instructions");

  const renamed = `Two edits renamed ${stamp()}`;
  await nameRow.getByRole("textbox", { name: "Project name" }).fill(renamed);
  await budgetRow.getByRole("textbox", { name: "Budget in USD" }).fill("40");
  await instructions.getByRole("textbox", { name: "Project instructions" }).fill("Be brief.");
  // Three dirty fields, three Saves: each belongs to its own section.
  await expect(
    page.getByRole("tabpanel", { name: "Settings" }).getByRole("button", { name: "Save" }),
  ).toHaveCount(3);

  await nameRow.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(renamed);
  await expect(instructions.getByRole("button", { name: "Save" })).toBeVisible();
  await expect(budgetRow.getByRole("textbox", { name: "Budget in USD" })).toHaveValue("40");
  await expect(budgetRow.getByRole("button", { name: "Save" })).toBeVisible();
});

test.describe("ownership", () => {
  test.skip(!mailAvailable(), "needs the stack's mail log (E2E_API_LOG)");

  test("transfers the project to a workspace member, and the old owner becomes an editor", async ({
    accountPage: page,
    account,
    browser,
    baseURL,
  }) => {
    const heir = await signUp("e2e-project-settings", { name: "Hal Heir" });
    await verifyEmail(account);
    await joinWorkspace(account, heir);
    const project = await createProject(account.token, `Handed over ${stamp()}`);
    await openSettings(page, project.id);

    const settings = page.getByRole("tabpanel", { name: "Settings" });
    await settings.getByRole("button", { name: "Transfer ownership" }).click();
    const dialog = page.getByRole("dialog", { name: "Transfer ownership" });
    const confirm = dialog.getByRole("button", { name: "Transfer ownership" });
    await expect(confirm).toBeDisabled();
    await dialog.getByRole("textbox", { name: "New owner's email" }).fill(heir.email);
    await confirm.click();
    await expect(toast(page, "Ownership transferred.")).toBeVisible();
    await expect(dialog).toBeHidden();

    // The old owner stays on as an editor, and every owner-only control is gone.
    const main = page.getByRole("main");
    await expect(main.getByText("editor", { exact: true })).toBeVisible();
    for (const title of ["General", "Budget", "Danger zone", "Invite links"])
      await expect(settings.getByRole("heading", { name: title, exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Change project icon" })).toHaveCount(0);
    await expect(main.getByRole("button", { name: "Invite", exact: true })).toHaveCount(0);
    const people = section(page, "People");
    await expect(people.getByRole("listitem").filter({ hasText: heir.email })).toContainText(
      "Owner",
    );
    await expect(people.getByRole("listitem").filter({ hasText: account.email })).toContainText(
      "Editor",
    );

    await page.reload();
    await expect(main.getByText("editor", { exact: true })).toBeVisible();
    await expect(settings.getByRole("heading", { name: "Danger zone" })).toHaveCount(0);

    // The new owner holds the owner's controls.
    const theirs = await pageFor(browser, heir, baseURL!);
    await openSettings(theirs, project.id);
    await expect(theirs.getByRole("main").getByText("owner", { exact: true })).toBeVisible();
    await expect(
      theirs
        .getByRole("tabpanel", { name: "Settings" })
        .getByRole("heading", { name: "Danger zone" }),
    ).toBeVisible();
    await theirs.context().close();
  });
});
