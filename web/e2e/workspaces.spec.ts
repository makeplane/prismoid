/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { Page } from "@playwright/test";
import { joinWorkspace, pageFor, PASSWORD, signUp, verifyEmail } from "./support/accounts";
import { call, stamp } from "./support/api";
import { codeIn, mailAvailable, waitForMail } from "./support/mailbox";
import { composer, expect, knownBug, openSettings, test, toast } from "./fixtures";

// Workspaces (ADR-061): the tenant. A person founds their own, switches between the ones they
// belong to, and brings others in by an emailed invite. A switch or a join rebinds the session, so
// every flow runs on fresh accounts, and each person carries a name of their own so "landed in the
// other workspace" can be told from "stayed home".

const WEB = "http://localhost:5304";

/** The switcher's face: the workspace this session works in, and the person's role there. */
function switcher(page: Page) {
  return page.getByRole("button", { name: "Switch workspace" });
}

/** Settings, on the Members pane. */
async function openMembers(page: Page, email: string) {
  const settings = await openSettings(page, email);
  await settings.getByRole("button", { name: "Members", exact: true }).click();
  await expect(settings.getByRole("heading", { name: "Members" })).toBeVisible();
  return settings;
}

/** Invite `email` to the owner's workspace through the API and read the emailed oi_ code. */
async function inviteCode(owner: { token: string; workspace: { id: string } }, email: string) {
  await call(`/workspaces/${owner.workspace.id}/invites`, {
    token: owner.token,
    body: { email, role: "member" },
  });
  return codeIn(await waitForMail(email, /invited/i), "oi_");
}

test.describe("workspaces", () => {
  test("founds a workspace, lands in it, and switches back", async ({
    accountPage: page,
    account,
  }) => {
    const name = `Tide Lab ${stamp()}`;
    await page.goto("/");
    await expect(switcher(page)).toContainText(account.workspace.name);

    await switcher(page).click();
    await page.getByRole("menuitem", { name: "Create workspace" }).click();
    const dialog = page.getByRole("dialog", { name: "Create workspace" });
    await dialog.getByRole("textbox", { name: "Workspace name" }).fill(name);
    await dialog.getByRole("button", { name: "Create" }).click();
    // The switch reloads the page fully, into the new workspace, as its owner.
    await expect(switcher(page)).toContainText(name);
    await expect(switcher(page)).toContainText("owner");
    await expect(composer(page)).toBeVisible();

    await switcher(page).click();
    await expect(page.getByRole("menuitemradio", { name: new RegExp(name) })).toBeChecked();
    await page.getByRole("menuitemradio", { name: new RegExp(account.workspace.name) }).click();
    await expect(switcher(page)).toContainText(account.workspace.name);
    await page.reload();
    await expect(switcher(page)).toContainText(account.workspace.name);
  });

  test.describe("invites", () => {
    test("an owner with a proven address invites by email and revokes it", async ({
      accountPage: page,
      account,
    }) => {
      test.skip(!mailAvailable(), "needs the stack's mail log (E2E_API_LOG)");
      await verifyEmail(account);
      const invitee = `e2e-workspaces-invitee-${stamp()}@example.com`;
      await page.goto("/");
      const settings = await openMembers(page, account.email);
      await settings.getByRole("button", { name: "Invite" }).click();
      const dialog = page.getByRole("dialog", { name: "Invite to this workspace" });
      await expect(dialog.getByRole("combobox", { name: "Role" })).toHaveText("Member");
      await dialog.getByRole("textbox", { name: "Email" }).fill(invitee);
      await dialog.getByRole("button", { name: "Send invite" }).click();
      await expect(toast(page, "Invite sent.")).toBeVisible();
      await expect(dialog).toBeHidden();

      await expect(settings.getByRole("heading", { name: "Pending invites" })).toBeVisible();
      const row = settings.getByRole("listitem").filter({ hasText: invitee });
      await expect(row).toContainText("member");
      await waitForMail(invitee, /invited/i);

      await row.getByRole("button", { name: "Revoke" }).click();
      await expect(row).toBeHidden();
      await expect(settings.getByRole("heading", { name: "Pending invites" })).toBeHidden();
      await page.reload();
      const reopened = await openMembers(page, account.email);
      await expect(reopened.getByText(invitee)).toBeHidden();
    });

    test("an unproven address cannot invite, and the dialog keeps what was typed", async ({
      accountPage: page,
      account,
    }) => {
      knownBug(
        "web/app/components/workspace/workspace-members.tsx:180 closes the invite dialog and " +
          "clears the email whatever the answer: onInvite's run() swallows the refusal into a " +
          "toast and resolves, so a refused invite looks sent and the typed address is lost",
      );
      const invitee = `e2e-workspaces-invitee-${stamp()}@example.com`;
      await page.goto("/");
      const settings = await openMembers(page, account.email);
      await settings.getByRole("button", { name: "Invite" }).click();
      const dialog = page.getByRole("dialog", { name: "Invite to this workspace" });
      await dialog.getByRole("textbox", { name: "Email" }).fill(invitee);
      await dialog.getByRole("button", { name: "Send invite" }).click();
      await expect(toast(page, "Verify your email before you invite anyone.")).toBeVisible();
      await expect(settings.getByRole("heading", { name: "Pending invites" })).toBeHidden();

      await expect(dialog).toBeVisible();
      await expect(dialog.getByRole("textbox", { name: "Email" })).toHaveValue(invitee);
    });

    test("a signed-in invitee accepts the emailed link", async ({ browser, account }) => {
      test.skip(!mailAvailable(), "needs the stack's mail log (E2E_API_LOG)");
      await verifyEmail(account);
      const invitee = await signUp("e2e-workspaces", { name: "Ivy Invitee" });
      const code = await inviteCode(account, invitee.email);
      const page = await pageFor(browser, invitee, WEB);

      await page.goto(`/invite/${code}`);
      await expect(page).toHaveURL(`${WEB}/`);
      await expect(switcher(page)).toContainText(account.workspace.name);
      await expect(switcher(page)).toContainText("member");
      await page.context().close();
    });

    test("a signed-out invitee signs up through the link and joins", async ({ page, account }) => {
      test.skip(!mailAvailable(), "needs the stack's mail log (E2E_API_LOG)");
      await verifyEmail(account);
      const email = `e2e-workspaces-new-${stamp()}@example.com`;
      const code = await inviteCode(account, email);

      await page.goto(`/invite/${code}`);
      await expect(page).toHaveURL(`/signup?invite=${code}`);
      await expect(page.getByRole("textbox", { name: "Invite code (optional)" })).toHaveValue(code);
      await page.getByRole("textbox", { name: "Name" }).fill("Ivy Invitee");
      await page.getByRole("textbox", { name: "Email" }).fill(email);
      await page.getByRole("textbox", { name: "Password" }).fill(PASSWORD);
      await page.getByRole("button", { name: "Create account" }).click();

      await expect(composer(page)).toBeVisible();
      await expect(switcher(page)).toContainText(account.workspace.name);
      await expect(switcher(page)).toContainText("member");
      const members = await call<{ user: { email: string } }[]>(
        `/workspaces/${account.workspace.id}/members`,
        { token: account.token },
      );
      expect(members.map((m) => m.user.email)).toContain(email);
    });

    test("joins with a code from the switcher", async ({ accountPage: page, account }) => {
      test.skip(!mailAvailable(), "needs the stack's mail log (E2E_API_LOG)");
      const owner = await signUp("e2e-workspaces", { name: "Olga Owner" });
      await verifyEmail(owner);
      const code = await inviteCode(owner, account.email);
      await page.goto("/");

      await switcher(page).click();
      await page.getByRole("menuitem", { name: "Join with a code" }).click();
      const dialog = page.getByRole("dialog", { name: "Join with a code" });
      const field = dialog.getByRole("textbox", { name: "Invite code" });
      // A code that was never issued is refused inside the dialog, in the server's words.
      await field.fill("oi_thisCodeWasNeverIssuedByTheServer");
      await dialog.getByRole("button", { name: "Join" }).click();
      await expect(dialog.getByText("this invite is invalid or has expired")).toBeVisible();
      await expect(field).toHaveAttribute("aria-invalid", "true");

      await field.fill(code);
      await dialog.getByRole("button", { name: "Join" }).click();
      await expect(switcher(page)).toContainText(owner.workspace.name);
      await expect(switcher(page)).toContainText("member");
    });
  });

  test.describe("members", () => {
    test.skip(!mailAvailable(), "needs the stack's mail log (E2E_API_LOG)");

    test("the owner changes a member's role, then removes them", async ({
      accountPage: page,
      account,
    }) => {
      await verifyEmail(account);
      const member = await signUp("e2e-workspaces", { name: "Mo Member" });
      await joinWorkspace(account, member);
      await page.goto("/");
      let settings = await openMembers(page, account.email);

      const role = settings.getByRole("combobox", { name: "Role for Mo Member" });
      await expect(role).toHaveText("Member");
      await role.click();
      await page.getByRole("option", { name: "Admin", exact: true }).click();
      await expect(role).toHaveText("Admin");
      await page.reload();
      settings = await openMembers(page, account.email);
      await expect(settings.getByRole("combobox", { name: "Role for Mo Member" })).toHaveText(
        "Admin",
      );

      const row = settings.getByRole("listitem").filter({ hasText: member.email });
      await row.getByRole("button", { name: "Remove" }).click();
      const confirm = page.getByRole("alertdialog", { name: `Remove ${member.email}?` });
      await confirm.getByRole("button", { name: "Remove" }).click();
      await expect(toast(page, "Removed.")).toBeVisible();
      await expect(row).toBeHidden();
      const members = await call<{ user: { email: string } }[]>(
        `/workspaces/${account.workspace.id}/members`,
        { token: account.token },
      );
      expect(members.map((m) => m.user.email)).not.toContain(member.email);
    });

    test("a member leaves and is back in their own workspace", async ({
      accountPage: page,
      account,
    }) => {
      const owner = await signUp("e2e-workspaces", { name: "Olga Owner" });
      await verifyEmail(owner);
      // Accepting moves this person's session into the owner's workspace.
      await joinWorkspace(owner, account);
      await page.goto("/");
      await expect(switcher(page)).toContainText(owner.workspace.name);

      const settings = await openMembers(page, account.email);
      // A member manages nobody: no invite, and only their own row has a verb.
      await expect(settings.getByRole("button", { name: "Invite" })).toBeHidden();
      await expect(settings.getByRole("button", { name: "Remove" })).toBeHidden();
      await settings
        .getByRole("listitem")
        .filter({ hasText: account.email })
        .getByRole("button", { name: "Leave" })
        .click();
      const confirm = page.getByRole("alertdialog", { name: "Leave this workspace?" });
      await confirm.getByRole("button", { name: "Leave" }).click();

      await expect(switcher(page)).toContainText(account.workspace.name);
      const members = await call<{ user: { email: string } }[]>(
        `/workspaces/${owner.workspace.id}/members`,
        { token: owner.token },
      );
      expect(members.map((m) => m.user.email)).not.toContain(account.email);
    });
  });
});
