/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { signUp } from "../support/accounts";
import { call, createProject, stamp } from "../support/api";
import { escapeRegExp, expect, test } from "../fixtures";

// The tenants and the folders inside them, observed read-only: a workspace's standing and a
// project's facts, members and tasks. Each flow signs up its own person, whose stamped name makes
// their workspace's row the only one of its kind in a list every run shares.

test.describe("console workspaces", () => {
  test("lists a fresh workspace and opens its standing", async ({ adminPage: page }) => {
    const person = await signUp("e2e-admin-ws", { name: `Wren Space ${stamp()}` });
    const workspace = person.workspace;
    await page.goto("/workspaces");
    const main = page.getByRole("main");
    // Newest first, and this one was made a moment ago, so it is on the first page.
    const row = main.getByRole("link", { name: new RegExp(`^${escapeRegExp(workspace.name)} `) });
    await expect(row).toBeVisible();
    await expect(row).toContainText("1 member");
    await expect(row).toContainText("Personal");
    await expect(row).toContainText("active");

    await row.click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${workspace.id}$`));
    await expect(main.getByRole("heading", { name: workspace.name, level: 1 })).toBeVisible();
    const standing = main.getByRole("paragraph").filter({ hasText: "created" });
    await expect(standing).toContainText("Personal");
    await expect(standing).toContainText("active");
    await expect(main.getByRole("heading", { name: "Standing", level: 2 })).toBeVisible();
    await expect(main.getByText(/Members\s*1/)).toBeVisible();
    await expect(main.getByRole("heading", { name: "History", level: 2 })).toBeVisible();
    await expect(main.getByText("No operator actions on this workspace yet")).toBeVisible();

    await main
      .getByRole("navigation", { name: "Breadcrumb" })
      .getByRole("link", { name: "Workspaces" })
      .click();
    await expect(page).toHaveURL(/\/workspaces$/);
  });
});

test.describe("console projects", () => {
  test("search finds a fresh project and its tabs show facts, members and tasks", async ({
    adminPage: page,
  }) => {
    const owner = await signUp("e2e-admin-projects", { name: `Otto Owner ${stamp()}` });
    const project = await createProject(owner.token, `Atlas ${stamp()}`);
    // A held task filed in the project: a routine fired with posture "hold" mints a real task
    // that no worker plans or runs.
    const goal = `Inventory the tide pools ${stamp()}`;
    const routine = await call<{ id: string }>("/routines", {
      token: owner.token,
      body: {
        name: `Held ${stamp()}`,
        instruction: goal,
        schedule: { cron: "0 9 1 1 *", timezone: "UTC" },
        posture: "hold",
        project_id: project.id,
      },
    });
    const fired = await call<{ task_id: string }>(`/routines/${routine.id}/fire`, {
      token: owner.token,
      body: {},
    });
    const task = await call<{ title: string }>(`/tasks/${fired.task_id}`, { token: owner.token });

    await page.goto("/projects");
    const search = page.getByRole("textbox", { name: "Search project name or ID…" });
    await search.fill(project.name);
    const main = page.getByRole("main");
    // Every project row says when it was made; once the search has landed (the box debounces),
    // this project's row is the only one.
    const rows = main.getByRole("link", { name: / · created / });
    const row = main.getByRole("link", { name: new RegExp(`^${escapeRegExp(project.name)} `) });
    await expect(row).toHaveCount(1);
    await expect(rows).toHaveCount(1);
    await expect(row).toContainText(owner.email);
    await expect(row).toContainText("1 task");
    // The id finds it too: the search rides the URL, so a reload runs it fresh.
    await search.fill(project.id);
    await expect(page).toHaveURL(new RegExp(`q=${project.id}`));
    await page.reload();
    await expect(search).toHaveValue(project.id);
    await expect(row).toHaveCount(1);
    await expect(rows).toHaveCount(1);
    await search.fill(`no such project ${stamp()}`);
    await expect(main.getByText(/No matches for/)).toBeVisible();
    await search.fill(project.name);

    await row.click();
    await expect(page).toHaveURL(new RegExp(`/projects/${project.id}$`));
    await expect(main.getByRole("heading", { name: project.name, level: 1 })).toBeVisible();
    await expect(main.getByRole("tab", { name: "Overview", selected: true })).toBeVisible();
    await expect(main.getByRole("heading", { name: "Facts", level: 2 })).toBeVisible();
    const facts = main.getByRole("listitem");
    await expect(facts.filter({ hasText: /^Tasks/ })).toContainText("1 task");
    await expect(facts.filter({ hasText: /^Spend this month/ })).toContainText("no budget set");

    await main.getByRole("tab", { name: "Tasks" }).click();
    await expect(page).toHaveURL(/tab=tasks/);
    const taskRow = main.getByRole("listitem").filter({ hasText: task.title });
    await expect(taskRow).toContainText("· held");

    await main.getByRole("tab", { name: "Members" }).click();
    await expect(page).toHaveURL(/tab=members/);
    const ownerRow = main.getByRole("listitem").filter({ hasText: "Owner" });
    await expect(ownerRow).toContainText(owner.name);
    await ownerRow.getByRole("link", { name: owner.email }).click();
    await expect(page).toHaveURL(new RegExp(`/users/${owner.id}$`));
    await expect(main.getByRole("heading", { name: owner.name, level: 1 })).toBeVisible();
  });
});
