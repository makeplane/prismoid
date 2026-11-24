/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { joinWorkspace, signUp, verifyEmail } from "./support/accounts";
import { call, createProject, stamp } from "./support/api";
import { mailAvailable } from "./support/mailbox";
import { expect, section, test } from "./fixtures";

/** A kit Section is a bare <section> under an h2: found by its heading, the only name it has. */

test("creates a project and opens it from the list", async ({ signedInPage: page }) => {
  const name = `Field research ${Date.now().toString(36)}`;
  await page.goto("/projects");
  await page.getByRole("button", { name: "New project" }).click();

  const dialog = page.getByRole("dialog", { name: "New project" });
  const create = dialog.getByRole("button", { name: "Create project" });
  await expect(create).toBeDisabled();
  await dialog.getByRole("textbox", { name: "Name" }).fill(name);
  await create.click();

  // Creating opens the new project.
  await expect(page).toHaveURL(/\/projects\/[0-9a-f-]{36}$/);
  const projectUrl = page.url();
  await expect(page.getByText(name).first()).toBeVisible();

  // The list carries it, and opening it from there reaches the same project.
  await page.getByRole("link", { name: "Projects" }).first().click();
  await expect(page).toHaveURL(/\/projects$/);
  await page
    .getByRole("link", { name: new RegExp(name) })
    .first()
    .click();
  await expect(page).toHaveURL(projectUrl);
});

test("a fresh account has no projects, and search narrows the list", async ({
  accountPage: page,
  account,
}) => {
  await page.goto("/projects");
  const main = page.getByRole("main");
  await expect(main.getByText("No projects yet")).toBeVisible();

  const tag = stamp();
  await createProject(account.token, `Tide pools ${tag}`);
  await createProject(account.token, `Lighthouse ${tag}`);
  await page.reload();
  await expect(main.getByRole("link", { name: new RegExp(`Tide pools ${tag}`) })).toBeVisible();
  await expect(main.getByRole("link", { name: new RegExp(`Lighthouse ${tag}`) })).toBeVisible();

  // The search is the server's: only the matching project stays.
  const search = page.getByRole("textbox", { name: "Search projects…" });
  await search.fill("Lighthouse");
  await expect(main.getByRole("link", { name: new RegExp(`Lighthouse ${tag}`) })).toBeVisible();
  await expect(main.getByRole("link", { name: new RegExp(`Tide pools ${tag}`) })).toBeHidden();

  await search.fill(`nothing-called-${tag}`);
  await expect(main.getByText(`No matches for “nothing-called-${tag}”`)).toBeVisible();
  await expect(main.getByText("No projects yet")).toBeHidden();

  await search.fill("");
  await expect(main.getByRole("link", { name: new RegExp(`Tide pools ${tag}`) })).toBeVisible();
});

test.describe("projects shared with you", () => {
  test.skip(!mailAvailable(), "needs the stack's mail log (E2E_API_LOG)");

  test("groups the list by role, and the grouping survives a reload", async ({
    accountPage: page,
    account,
  }) => {
    // Someone else's workspace holds one project they share for editing and one for reading;
    // joining it moves this account there, where it then makes a project of its own.
    const host = await signUp("e2e-projects", { name: "Bea Host" });
    await verifyEmail(host);
    await joinWorkspace(host, account);
    const tag = stamp();
    const edited = await createProject(host.token, `Shared draft ${tag}`);
    const read = await createProject(host.token, `Shared notes ${tag}`);
    await call(`/projects/${edited.id}/members`, {
      token: host.token,
      body: { email: account.email, role: "editor" },
    });
    await call(`/projects/${read.id}/members`, {
      token: host.token,
      body: { email: account.email, role: "viewer" },
    });
    const own = await createProject(account.token, `My own ${tag}`);

    await page.goto("/projects");
    const main = page.getByRole("main");
    await expect(
      main.getByRole("link", { name: new RegExp(`Shared draft ${tag}.*editor`) }),
    ).toBeVisible();
    await expect(
      main.getByRole("link", { name: new RegExp(`Shared notes ${tag}.*viewer`) }),
    ).toBeVisible();
    await expect(
      main.getByRole("link", { name: new RegExp(`My own ${tag}.*owner`) }),
    ).toBeVisible();

    await page.getByRole("button", { name: "Display options" }).click();
    await page.getByRole("menuitemradio", { name: "Role" }).click();
    await page.keyboard.press("Escape");
    await expect(page).toHaveURL(/[?&]group=role/);

    // Ownership first, then participation; each project sits under its own role.
    await expect(main.getByRole("heading", { level: 2 })).toHaveText([
      "owners",
      "editors",
      "viewers",
    ]);
    await expect(
      section(page, "owners").getByRole("link", { name: new RegExp(own.name) }),
    ).toBeVisible();
    await expect(
      section(page, "editors").getByRole("link", { name: new RegExp(edited.name) }),
    ).toBeVisible();
    await expect(
      section(page, "viewers").getByRole("link", { name: new RegExp(read.name) }),
    ).toBeVisible();

    // The grouping rides the URL, so a reload keeps it; None takes it off again.
    await page.reload();
    await expect(main.getByRole("heading", { level: 2 })).toHaveText([
      "owners",
      "editors",
      "viewers",
    ]);
    await page.getByRole("button", { name: "Display options" }).click();
    await page.getByRole("menuitemradio", { name: "None" }).click();
    await page.keyboard.press("Escape");
    await expect(page).not.toHaveURL(/group=role/);
    await expect(main.getByRole("heading", { level: 2 })).toHaveCount(0);
    await expect(main.getByRole("link", { name: new RegExp(read.name) })).toBeVisible();
  });
});

test("the project page shows its tabs, and the owner's icon pick persists", async ({
  accountPage: page,
  account,
}) => {
  const project = await createProject(account.token, `Harbour survey ${stamp()}`);
  await page.goto(`/projects/${project.id}`);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(project.name);

  const tabs = page.getByRole("tablist");
  await expect(tabs.getByRole("tab")).toHaveText(["Tasks", "Artifacts", "Settings"]);
  await expect(tabs.getByRole("tab", { name: "Tasks" })).toHaveAttribute("aria-selected", "true");

  await tabs.getByRole("tab", { name: "Artifacts" }).click();
  const artifacts = page.getByRole("tabpanel", { name: "Artifacts" });
  await expect(artifacts.getByText("No artifacts yet")).toBeVisible();
  await expect(artifacts.getByRole("textbox", { name: "Search artifacts…" })).toBeVisible();

  await tabs.getByRole("tab", { name: "Settings" }).click();
  await expect(page.getByRole("tabpanel", { name: "Settings" })).toBeVisible();

  // The face is the owner's to change: pick a glyph and a colour, each saved as it lands.
  await page.getByRole("button", { name: "Change project icon" }).click();
  const picker = page
    .getByRole("dialog")
    .filter({ has: page.getByRole("textbox", { name: "Search icons…" }) });
  const saved = () =>
    page.waitForResponse(
      (r) => r.url().endsWith(`/v1/projects/${project.id}`) && r.request().method() === "PATCH",
    );
  let patched = saved();
  await picker.getByRole("button", { name: "rocket", exact: true }).click();
  expect((await patched).ok()).toBe(true);
  patched = saved();
  await picker.getByRole("radio", { name: "Color 3" }).click();
  expect((await patched).ok()).toBe(true);
  await expect(
    picker.getByRole("button", { name: "rocket", exact: true, pressed: true }),
  ).toBeVisible();

  await page.reload();
  await page.getByRole("button", { name: "Change project icon" }).click();
  await expect(
    picker.getByRole("button", { name: "rocket", exact: true, pressed: true }),
  ).toBeVisible();
  await expect(picker.getByRole("radio", { name: "Color 3" })).toBeChecked();
  await expect(picker.getByRole("radio", { name: "Neutral" })).not.toBeChecked();
});
