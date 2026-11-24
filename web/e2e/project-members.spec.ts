/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { Page } from "@playwright/test";
import { type Account, joinWorkspace, pageFor, signUp, verifyEmail } from "./support/accounts";
import { call, createProject, stamp } from "./support/api";
import { mailAvailable } from "./support/mailbox";
import { escapeRegExp, expect, section, test } from "./fixtures";

// A project's people (ADR-062): the owner adds someone from the workspace by email or hands them
// a single-use invite link, changes their role and removes them. Every person here must belong to
// the owner's workspace first, which takes the emailed workspace invite.
test.describe("project members", () => {
  test.skip(!mailAvailable(), "needs the stack's mail log (E2E_API_LOG)");

  /** A kit Section is a bare <section> under an h2: found by its heading, the only name it has. */

  /** More people, already in the owner's workspace (an owner must prove the address first). */
  async function team(owner: Account, ...names: string[]): Promise<Account[]> {
    await verifyEmail(owner);
    const people: Account[] = [];
    for (const name of names) {
      const them = await signUp("e2e-project-members", { name });
      await joinWorkspace(owner, them);
      people.push(them);
    }
    return people;
  }

  async function share(owner: Account, projectId: string, email: string, role: string) {
    await call(`/projects/${projectId}/members`, { token: owner.token, body: { email, role } });
  }

  test("the owner invites a teammate, who sees the project and a knock in the inbox", async ({
    accountPage: page,
    account,
    browser,
    baseURL,
  }) => {
    const [mate] = await team(account, "Max Member");
    const project = await createProject(account.token, `Estuary ${stamp()}`);
    await page.goto(`/projects/${project.id}`);
    await expect(page.getByRole("group", { name: "1 person" })).toBeVisible();

    // On the Tasks tab the header's Invite is the only one.
    await page.getByRole("button", { name: "Invite", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Invite to this project" });
    const send = dialog.getByRole("button", { name: "Send invite" });
    await expect(send).toBeDisabled();
    await expect(dialog.getByRole("combobox", { name: "Role" })).toHaveText("Editor");
    await dialog.getByRole("textbox", { name: "Email" }).fill(mate.email);
    await send.click();
    await expect(dialog).toBeHidden();
    await expect(page.getByRole("group", { name: "2 people" })).toBeVisible();
    await expect(page.getByRole("img", { name: "Max Member" })).toBeVisible();

    const theirs = await pageFor(browser, mate, baseURL!);
    await theirs.goto("/projects");
    await expect(
      theirs.getByRole("main").getByRole("link", { name: new RegExp(`${project.name}.*editor`) }),
    ).toBeVisible();

    // The share knocks their inbox, and its Open lands on the project.
    await theirs.goto("/inbox");
    const knock = `${account.name} added you to ${project.name}`;
    await theirs.getByRole("button", { name: new RegExp(knock) }).click();
    const main = theirs.getByRole("main");
    await expect(main.getByRole("heading", { level: 2, name: knock })).toBeVisible();
    await expect(main.getByText(/^Projects · /)).toBeVisible();
    await theirs.getByRole("button", { name: "Open project" }).click();
    await expect(theirs).toHaveURL(new RegExp(`/projects/${project.id}$`));
    await expect(theirs.getByRole("heading", { level: 1 })).toHaveText(project.name);
    await theirs.context().close();
  });

  test("the owner changes a member's role, then removes them", async ({
    accountPage: page,
    account,
    browser,
    baseURL,
  }) => {
    const [mate] = await team(account, "Rae Member");
    const project = await createProject(account.token, `Salt marsh ${stamp()}`);
    await page.goto(`/projects/${project.id}?tab=settings`);

    // On Settings the People section carries its own Invite beside the header's.
    const people = section(page, "People");
    await people.getByRole("button", { name: "Invite", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Invite to this project" });
    await dialog.getByRole("textbox", { name: "Email" }).fill(mate.email);
    await dialog.getByRole("combobox", { name: "Role" }).click();
    await page.getByRole("option", { name: /^Viewer/ }).click();
    await dialog.getByRole("button", { name: "Send invite" }).click();
    await expect(dialog).toBeHidden();

    const row = people.getByRole("listitem").filter({ hasText: mate.email });
    await expect(row.getByRole("combobox", { name: "Role" })).toHaveText("Viewer");
    await row.getByRole("combobox", { name: "Role" }).click();
    await page.getByRole("option", { name: "Editor" }).click();
    await expect(row.getByRole("combobox", { name: "Role" })).toHaveText("Editor");
    await page.reload();
    await expect(row.getByRole("combobox", { name: "Role" })).toHaveText("Editor");

    const theirs = await pageFor(browser, mate, baseURL!);
    await theirs.goto(`/projects/${project.id}`);
    await expect(theirs.getByRole("main").getByText("editor", { exact: true })).toBeVisible();

    await row.getByRole("button", { name: "Remove Rae Member" }).click();
    await expect(row).toHaveCount(0);
    await expect(page.getByRole("group", { name: "1 person" })).toBeVisible();
    await page.reload();
    await expect(people.getByRole("listitem").filter({ hasText: mate.email })).toHaveCount(0);

    // Removed, they no longer see it at all.
    await theirs.goto("/projects");
    await expect(theirs.getByRole("main").getByText("No projects yet")).toBeVisible();
    await theirs.context().close();
  });

  test("a viewer reads the project but gets no composer and no owner controls", async ({
    account,
    browser,
    baseURL,
  }) => {
    const [mate] = await team(account, "Vic Viewer");
    const project = await createProject(account.token, `Read only ${stamp()}`);
    await share(account, project.id, mate.email, "viewer");

    const theirs = await pageFor(browser, mate, baseURL!);
    await theirs.goto(`/projects/${project.id}`);
    const main = theirs.getByRole("main");
    // The viewer's own signals first, so the absences below are not just a slow mount.
    await expect(main.getByText("viewer", { exact: true })).toBeVisible();
    await expect(main.getByText("Tasks started in this Project show up here.")).toBeVisible();
    await expect(main.getByRole("textbox", { name: /^Describe what you need done/ })).toHaveCount(
      0,
    );
    await expect(main.getByRole("button", { name: "Invite", exact: true })).toHaveCount(0);
    await expect(main.getByRole("button", { name: "Change project icon" })).toHaveCount(0);

    await main.getByRole("tab", { name: "Settings" }).click();
    const settings = theirs.getByRole("tabpanel", { name: "Settings" });
    await expect(settings.getByText("No standing instructions yet.")).toBeVisible();
    await expect(settings.getByText("Nothing remembered here yet")).toBeVisible();
    await expect(settings.getByRole("listitem").filter({ hasText: mate.email })).toContainText(
      "Viewer",
    );
    for (const title of ["Invite links", "General", "Budget", "Danger zone"])
      await expect(settings.getByRole("heading", { name: title, exact: true })).toHaveCount(0);
    await expect(settings.getByRole("textbox")).toHaveCount(0);
    await expect(settings.getByRole("combobox")).toHaveCount(0);
    await theirs.context().close();
  });

  test.describe("invite links", () => {
    /** Mint a link from the owner's Settings tab and return the path it opens. */
    async function createLink(page: Page, role: "Editor" | "Viewer") {
      const links = section(page, "Invite links");
      await links.getByRole("button", { name: "Create link" }).click();
      const dialog = page.getByRole("dialog", { name: "Create an invite link" });
      await expect(dialog.getByRole("combobox", { name: "Expires" })).toHaveText("7 days");
      if (role === "Viewer") {
        await dialog.getByRole("combobox", { name: "Role" }).click();
        await page.getByRole("option", { name: /^Viewer/ }).click();
      }
      await dialog.getByRole("button", { name: "Create link" }).click();
      const field = dialog.getByRole("textbox", { name: "Invite link" });
      await expect(field).toHaveValue(/\/join\/si_[A-Za-z0-9_-]+$/);
      await expect(dialog.getByText("This link is shown once.")).toBeVisible();
      const path = new URL(await field.inputValue()).pathname;
      await dialog.getByRole("button", { name: "Close" }).click();
      await expect(dialog).toBeHidden();
      return path;
    }

    test("a teammate who opens the link signed in joins at its role, once", async ({
      accountPage: page,
      account,
      browser,
      baseURL,
    }) => {
      const [mate, late] = await team(account, "Lin Linker", "Lou Late");
      const project = await createProject(account.token, `Linked ${stamp()}`);
      await page.goto(`/projects/${project.id}?tab=settings`);
      const links = section(page, "Invite links");
      await expect(links.getByText(/No invite links yet/)).toBeVisible();

      const path = await createLink(page, "Viewer");
      const row = links.getByRole("listitem");
      await expect(row).toHaveCount(1);
      await expect(row).toContainText("Viewer");
      await expect(row.getByText("Live", { exact: true })).toBeVisible();
      // The list carries the link's state, never its code.
      await expect(links.getByText(/si_/)).toHaveCount(0);

      const theirs = await pageFor(browser, mate, baseURL!);
      await theirs.goto(path);
      await expect(theirs).toHaveURL(new RegExp(`/projects/${project.id}$`));
      await expect(theirs.getByRole("heading", { level: 1 })).toHaveText(project.name);
      await expect(theirs.getByRole("main").getByText("viewer", { exact: true })).toBeVisible();

      await page.reload();
      await expect(row.getByText("Used", { exact: true })).toBeVisible();
      await expect(row.getByRole("button", { name: /^Revoke / })).toHaveCount(0);
      await expect(
        section(page, "People").getByRole("listitem").filter({ hasText: mate.email }),
      ).toBeVisible();

      // Single use: a second person holding the same link is turned away.
      const lateTab = await pageFor(browser, late, baseURL!);
      await lateTab.goto(path);
      await expect(lateTab.getByRole("heading", { level: 1, name: "Couldn't join" })).toBeVisible();
      await lateTab.context().close();
      await theirs.context().close();
    });

    test("signed out, the link goes through sign-in and then joins", async ({
      accountPage: page,
      account,
      browser,
      baseURL,
    }) => {
      const [mate] = await team(account, "Sid Signin");
      const project = await createProject(account.token, `Via login ${stamp()}`);
      await page.goto(`/projects/${project.id}?tab=settings`);
      const path = await createLink(page, "Editor");

      const context = await browser.newContext({ baseURL });
      const anon = await context.newPage();
      await anon.goto(path);
      await expect(anon).toHaveURL(
        new RegExp(`/login\\?next=${escapeRegExp(encodeURIComponent(path))}$`),
      );
      await anon.getByRole("textbox", { name: "Email" }).fill(mate.email);
      await anon.getByRole("textbox", { name: "Password" }).fill(mate.password);
      await anon.getByRole("button", { name: "Sign in" }).click();
      await expect(anon).toHaveURL(new RegExp(`/projects/${project.id}$`));
      await expect(anon.getByRole("main").getByText("editor", { exact: true })).toBeVisible();
      await context.close();

      await page.reload();
      await expect(section(page, "Invite links").getByText("Used", { exact: true })).toBeVisible();
    });

    test("a revoked link stops working", async ({
      accountPage: page,
      account,
      browser,
      baseURL,
    }) => {
      const [mate] = await team(account, "Ren Revoked");
      const project = await createProject(account.token, `Revoked ${stamp()}`);
      await page.goto(`/projects/${project.id}?tab=settings`);
      const links = section(page, "Invite links");
      const kept = await createLink(page, "Viewer");
      const revoked = await createLink(page, "Editor");
      await expect(links.getByRole("listitem")).toHaveCount(2);

      await links.getByRole("button", { name: "Revoke editor link" }).click();
      const editorRow = links.getByRole("listitem").filter({ hasText: "Editor" });
      await expect(editorRow.getByText("Revoked", { exact: true })).toBeVisible();
      await expect(
        links
          .getByRole("listitem")
          .filter({ hasText: "Viewer" })
          .getByText("Live", { exact: true }),
      ).toBeVisible();
      await page.reload();
      await expect(editorRow.getByText("Revoked", { exact: true })).toBeVisible();

      const theirs = await pageFor(browser, mate, baseURL!);
      await theirs.goto(revoked);
      await expect(theirs.getByRole("heading", { level: 1, name: "Couldn't join" })).toBeVisible();
      // The link left alive still lets them in.
      await theirs.goto(kept);
      await expect(theirs).toHaveURL(new RegExp(`/projects/${project.id}$`));
      await theirs.context().close();
    });
  });
});

test("a link that was never issued says it couldn't join", async ({ accountPage: page }) => {
  await page.goto("/join/si_bogus");
  await expect(page.getByRole("heading", { level: 1, name: "Couldn't join" })).toBeVisible();
  await expect(page.getByText("this invite is invalid or has expired")).toBeVisible();
  await page.getByRole("button", { name: "Go home" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("textbox", { name: /Describe what you need done/ })).toBeVisible();
});
