/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { Page } from "@playwright/test";
import { joinWorkspace, signUp, verifyEmail } from "./support/accounts";
import { call, createHeldTask, createProject, type HeldTask, stamp } from "./support/api";
import { mailAvailable } from "./support/mailbox";
import { expect, test } from "./fixtures";

// The inbox: notifications on the left, what each is about on the right. A held routine run is
// the knock a flow can make without a model: firing a held routine mints a task and tells its
// owner it is waiting for them.

/** The sidebar's Inbox entry, whose text carries the unread count beside the word. */
function inboxEntry(page: Page) {
  return page
    .getByRole("listitem")
    .filter({ has: page.getByRole("link", { name: "Inbox", exact: true }) });
}

/** A held run's row in the list, by the routine that holds it. */
function heldRow(page: Page, task: HeldTask) {
  return page
    .getByRole("main")
    .getByRole("button", { name: new RegExp(`${task.routine.name} is holding a run for you`) });
}

/** Two held runs, oldest first: the list shows the newest at the top. */
async function twoHeld(token: string): Promise<[HeldTask, HeldTask]> {
  const older = await createHeldTask(token, `Count the gulls ${stamp()}`);
  const newer = await createHeldTask(token, `Count the terns ${stamp()}`);
  return [older, newer];
}

test("a held run knocks the inbox, and Open task goes to it", async ({
  accountPage: page,
  account,
}) => {
  const task = await createHeldTask(account.token, `Summarize the tide pool notes ${stamp()}`);
  await page.goto("/inbox");
  await expect(inboxEntry(page)).toHaveText(/^Inbox\s*1$/);

  // The first notification is selected on arrival: the right pane says what it is about.
  const row = heldRow(page, task);
  await expect(row).toHaveAttribute("aria-current", "true");
  const main = page.getByRole("main");
  const title = `${task.routine.name} is holding a run for you`;
  await expect(main.getByRole("heading", { level: 2, name: title })).toBeVisible();
  await expect(main.getByText(/^Held runs · /)).toBeVisible();
  await expect(main.getByText(task.goal).first()).toBeVisible();

  await page.getByRole("button", { name: "Open task" }).click();
  await expect(page).toHaveURL(new RegExp(`/tasks/${task.id}$`));
});

test("reading clears the badge one by one, and Mark all read clears the rest", async ({
  accountPage: page,
  account,
}) => {
  const [older, newer] = await twoHeld(account.token);
  await page.goto("/inbox");
  await expect(inboxEntry(page)).toHaveText(/^Inbox\s*2$/);
  await expect(heldRow(page, newer)).toHaveAttribute("aria-current", "true");

  await heldRow(page, older).click();
  await expect(heldRow(page, older)).toHaveAttribute("aria-current", "true");
  await expect(
    page.getByRole("main").getByRole("heading", {
      level: 2,
      name: `${older.routine.name} is holding a run for you`,
    }),
  ).toBeVisible();
  await expect(inboxEntry(page)).toHaveText(/^Inbox\s*1$/);

  const markAll = page.getByRole("button", { name: "Mark all read" });
  await markAll.click();
  await expect(markAll).toBeDisabled();
  // The badge moves only after the server took it, so a reload after it reads the server's answer.
  await expect(inboxEntry(page)).toHaveText(/^Inbox$/);
  await page.reload();
  await expect(inboxEntry(page)).toHaveText(/^Inbox$/);
  await expect(page.getByRole("button", { name: "Mark all read" })).toBeDisabled();
});

test("Unread only shows what is left, then the caught-up state", async ({
  accountPage: page,
  account,
}) => {
  const [older, newer] = await twoHeld(account.token);
  await page.goto("/inbox");
  await heldRow(page, older).click();
  await expect(inboxEntry(page)).toHaveText(/^Inbox\s*1$/);

  await page.getByRole("button", { name: "Display options" }).click();
  const unreadOnly = page.getByRole("menuitemcheckbox", { name: /^Unread only/ });
  await expect(unreadOnly).toHaveAccessibleName("Unread only 1");
  await unreadOnly.click();
  await expect(unreadOnly).toBeChecked();
  await page.keyboard.press("Escape");
  await expect(heldRow(page, newer)).toBeVisible();
  await expect(heldRow(page, older)).toBeHidden();

  await page.getByRole("button", { name: "Mark all read" }).click();
  await expect(page.getByRole("main").getByText("You're all caught up")).toBeVisible();
  await expect(heldRow(page, newer)).toBeHidden();

  // Taking the view off brings every notification back.
  await page.getByRole("button", { name: "Display options" }).click();
  await page.getByRole("menuitemcheckbox", { name: /^Unread only/ }).click();
  await page.keyboard.press("Escape");
  await expect(heldRow(page, newer)).toBeVisible();
  await expect(heldRow(page, older)).toBeVisible();
});

test("the arrow keys move the selection and Enter opens it", async ({
  accountPage: page,
  account,
}) => {
  const [older, newer] = await twoHeld(account.token);
  await page.goto("/inbox");
  await expect(heldRow(page, newer)).toHaveAttribute("aria-current", "true");

  await page.keyboard.press("ArrowDown");
  await expect(heldRow(page, older)).toHaveAttribute("aria-current", "true");
  await expect(heldRow(page, newer)).toHaveAttribute("aria-current", "false");
  // The last row is the floor: another ArrowDown stays put.
  await page.keyboard.press("ArrowDown");
  await expect(heldRow(page, older)).toHaveAttribute("aria-current", "true");
  await page.keyboard.press("ArrowUp");
  await expect(heldRow(page, newer)).toHaveAttribute("aria-current", "true");
  await page.keyboard.press("ArrowDown");
  await expect(heldRow(page, older)).toHaveAttribute("aria-current", "true");

  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(new RegExp(`/tasks/${older.id}$`));
});

test("Enter on a focused toolbar button works that button, not the selected item", async ({
  accountPage: page,
  account,
}) => {
  await createHeldTask(account.token, `Hold the pier survey ${stamp()}`);
  await page.goto("/inbox");
  await expect(page.getByRole("button", { name: "Open task" })).toBeVisible();

  const filter = page.getByRole("button", { name: "Filter by kind" });
  await filter.focus();
  // Start listening before the key: a navigation that commits a moment later still counts.
  const navigated = page.waitForURL(/\/tasks\//, { timeout: 3000 }).then(
    () => true,
    () => false,
  );
  await page.keyboard.press("Enter");
  await expect(page.getByRole("menu", { name: "Filter by kind" })).toBeVisible();
  expect(await navigated).toBe(false);
  await expect(page).toHaveURL(/\/inbox$/);
});

test.describe("project knocks", () => {
  test.skip(!mailAvailable(), "needs the stack's mail log (E2E_API_LOG)");

  test("Filter by kind lists the kinds present and narrows to one", async ({
    accountPage: page,
    account,
  }) => {
    const host = await signUp("e2e-inbox", { name: "Bea Host" });
    await verifyEmail(host);
    await joinWorkspace(host, account);
    const project = await createProject(host.token, `Shared shoreline ${stamp()}`);
    await call(`/projects/${project.id}/members`, {
      token: host.token,
      body: { email: account.email, role: "editor" },
    });
    const held = await createHeldTask(account.token, `Hold the dune count ${stamp()}`);
    const sharedRow = page
      .getByRole("main")
      .getByRole("button", { name: new RegExp(`Bea Host added you to ${project.name}`) });

    await page.goto("/inbox");
    await expect(sharedRow).toBeVisible();
    await expect(heldRow(page, held)).toBeVisible();

    const filter = page.getByRole("button", { name: "Filter by kind" });
    await filter.click();
    const menu = page.getByRole("menu", { name: "Filter by kind" });
    await expect(menu.getByRole("menuitemradio")).toHaveText([
      "All notifications",
      "Projects",
      "Held runs",
    ]);
    await menu.getByRole("menuitemradio", { name: "Held runs" }).click();
    await page.keyboard.press("Escape");
    await expect(heldRow(page, held)).toBeVisible();
    await expect(sharedRow).toBeHidden();

    await filter.click();
    await menu.getByRole("menuitemradio", { name: "Projects" }).click();
    await page.keyboard.press("Escape");
    await expect(sharedRow).toBeVisible();
    await expect(heldRow(page, held)).toBeHidden();

    await filter.click();
    await menu.getByRole("menuitemradio", { name: "All notifications" }).click();
    await page.keyboard.press("Escape");
    await expect(sharedRow).toBeVisible();
    await expect(heldRow(page, held)).toBeVisible();
  });

  test("being added to and removed from a project never shows two identical filters", async ({
    accountPage: page,
    account,
  }) => {
    const host = await signUp("e2e-inbox", { name: "Bea Host" });
    await verifyEmail(host);
    await joinWorkspace(host, account);
    const project = await createProject(host.token, `Brief stay ${stamp()}`);
    await call(`/projects/${project.id}/members`, {
      token: host.token,
      body: { email: account.email, role: "editor" },
    });
    await call(`/projects/${project.id}/members/${account.id}`, {
      token: host.token,
      method: "DELETE",
    });

    await page.goto("/inbox");
    const main = page.getByRole("main");
    const added = main.getByRole("button", { name: new RegExp(`added you to ${project.name}`) });
    const removed = main.getByRole("button", {
      name: new RegExp(`removed you from ${project.name}`),
    });
    await expect(added).toBeVisible();
    await expect(removed).toBeVisible();

    // Every filter must be one a person can tell apart from the rest.
    await page.getByRole("button", { name: "Filter by kind" }).click();
    const menu = page.getByRole("menu", { name: "Filter by kind" });
    await expect(menu.getByRole("menuitemradio", { name: "All notifications" })).toBeVisible();
    const labels = await menu.getByRole("menuitemradio").allTextContents();
    expect(labels.length).toBeGreaterThan(1);
    expect(new Set(labels).size, `filters: ${labels.join(", ")}`).toBe(labels.length);
  });
});
