/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { expect, knownBug, test, toast } from "./fixtures";
import { ApiError, call, createBot, stamp } from "./support/api";

// A bot's tabs, everything that needs no model: the empty shelves, memory, the webhook door and
// the chat's threads. No message is ever typed into a chat: a sent line asks the bot to answer.

/** A memory the bot keeps, added by hand through the API. */
async function teach(token: string, bot: string, text: string): Promise<void> {
  await call("/memories", { token, body: { scope: "bot", bot_name: bot, kind: "fact", text } });
}

/** Reads a webhook channel's reply feed with its own token: the one thing the token opens. */
async function readFeed(channelId: string, token: string): Promise<number> {
  try {
    await call(`/channels/${channelId}/messages`, { token });
    return 200;
  } catch (e) {
    if (e instanceof ApiError) return e.status;
    throw e;
  }
}

test.describe("bots: tabs", () => {
  test("each tab opens from the rail and names its empty state", async ({
    accountPage: page,
    account,
  }) => {
    const { name } = await createBot(account.token, { name: `shelves-${stamp()}` });
    await page.goto(`/bots/${name}`);
    const main = page.getByRole("main");
    const tab = (label: string) => main.getByRole("tab", { name: label, exact: true });
    await expect(tab("Chat")).toHaveAttribute("aria-selected", "true");

    await tab("Runs").click();
    await expect(page).toHaveURL(/\?tab=runs$/);
    await expect(main.getByText("No runs yet")).toBeVisible();

    await tab("Artifacts").click();
    await expect(page).toHaveURL(/\?tab=artifacts$/);
    await expect(main.getByText("Nothing published yet")).toBeVisible();

    await tab("Routines").click();
    await expect(page).toHaveURL(/\?tab=routines$/);
    await expect(main.getByText("No standing work yet")).toBeVisible();
    await expect(main.getByRole("link", { name: "Open routines…" })).toHaveAttribute(
      "href",
      "/routines",
    );

    await tab("Access").click();
    await expect(page).toHaveURL(/\?tab=access$/);
    await expect(main.getByRole("heading", { name: "Tools" })).toBeVisible();
    await expect(main.getByRole("listitem").filter({ hasText: "ask_user" })).toContainText(
      "Ask you questions mid-task",
    );
    await expect(main.getByText("No connectors declared")).toBeVisible();

    // The tab rides the URL: a reload lands on the same tab.
    await page.reload();
    await expect(tab("Access")).toHaveAttribute("aria-selected", "true");
    await tab("Chat").click();
    await expect(page).toHaveURL(new RegExp(`/bots/${name}$`));
  });

  test("teaches a memory and finds it", async ({ accountPage: page, account }) => {
    const { name } = await createBot(account.token, { name: `memo-${stamp()}` });
    await page.goto(`/bots/${name}?tab=memory`);
    const main = page.getByRole("main");
    await expect(main.getByText("Nothing learned yet")).toBeVisible();

    await main.getByRole("button", { name: "Add memory" }).click();
    const dialog = page.getByRole("dialog", { name: `Teach ${name} something` });
    const add = dialog.getByRole("button", { name: "Add memory" });
    await expect(add).toBeDisabled();
    await dialog.getByRole("textbox", { name: "Memory" }).fill("The Q3 workbook lives in finance.");
    await dialog.getByRole("button", { name: "fact", exact: true }).click();
    await expect(dialog.getByRole("button", { name: "fact", exact: true })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await add.click();
    await expect(dialog).toBeHidden();

    const row = main.getByRole("listitem").filter({ hasText: "The Q3 workbook lives in finance." });
    await expect(row).toContainText("Fact");
    await expect(row).toContainText("added by hand");
    await page.reload();
    await expect(row).toBeVisible();

    const search = main.getByRole("textbox", { name: "Search memories…" });
    await search.fill("workbook");
    await expect(row).toBeVisible();
    await search.fill("payroll");
    await expect(main.getByText("No matches for “payroll”")).toBeVisible();
    await search.fill("");
    await expect(row).toBeVisible();
  });

  test("edits a memory in place", async ({ accountPage: page, account }) => {
    knownBug(
      "backend/prismoid/db/models/memory.py:245 - updated_at's onupdate=func.now() leaves the " +
        "attribute expired after the commit, so memory_out (api/serializers/memories.py:83) " +
        "lazy-loads it outside the greenlet: PATCH /v1/memories/{id} saves, then answers 500 " +
        "(MissingGreenlet) and the row stays in edit mode with 'Couldn't save the memory.'",
    );
    const { name } = await createBot(account.token, { name: `memoedit-${stamp()}` });
    await teach(account.token, name, "The Q3 workbook lives in finance.");
    await page.goto(`/bots/${name}?tab=memory`);
    const main = page.getByRole("main");
    const row = main.getByRole("listitem").filter({ hasText: "The Q3 workbook lives in finance." });
    // The row's verbs show on hover. While it edits, the text sits in an input, so the row is
    // found by its provenance line instead.
    const editing = main.getByRole("listitem").filter({ hasText: "added by hand" });
    await row.hover();
    await row.getByRole("button", { name: "Edit" }).click();
    await editing.getByRole("textbox").fill("The Q3 workbook lives in the finance project.");
    await editing.getByRole("button", { name: "Save" }).click();
    await expect(
      main
        .getByRole("listitem")
        .filter({ hasText: "The Q3 workbook lives in the finance project." }),
    ).toBeVisible();
  });

  test("retires a memory", async ({ accountPage: page, account }) => {
    knownBug(
      "backend/prismoid/db/models/memory.py:245 - the same expired updated_at: POST " +
        "/v1/memories/{id}/retire retires the row, then answers 500 (MissingGreenlet in " +
        "memory_out), so the row stays listed with 'Couldn't retire the memory.' until a reload",
    );
    const { name } = await createBot(account.token, { name: `memoretire-${stamp()}` });
    await teach(account.token, name, "Invoices go out on the first.");
    await page.goto(`/bots/${name}?tab=memory`);
    const main = page.getByRole("main");
    const row = main.getByRole("listitem").filter({ hasText: "Invoices go out on the first." });
    await row.hover();
    await row.getByRole("button", { name: "Retire" }).click();
    await expect(main.getByText("Nothing learned yet")).toBeVisible();
  });

  test("mints a webhook door, disables, rotates and removes it", async ({
    accountPage: page,
    account,
  }) => {
    const { name } = await createBot(account.token, { name: `door-${stamp()}` });
    await page.goto(`/bots/${name}?tab=channels`);
    const main = page.getByRole("main");
    await expect(main.getByText("No doors yet")).toBeVisible();

    await main.getByRole("button", { name: "Add webhook" }).click();
    await expect(
      main.getByText("Token, shown once - store it where the caller lives:"),
    ).toBeVisible();
    const token = main.getByRole("code").filter({ hasText: /^bc_/ });
    const first = (await token.textContent())!;
    const howTo = main.getByText(/^POST \/v1\/channels\/[0-9a-f-]+\/messages/);
    const channelId = (await howTo.textContent())!.match(/channels\/([0-9a-f-]+)\//)![1];
    // The token is real: it opens this channel's reply feed (a read, which asks nothing of the bot).
    expect(await readFeed(channelId, first)).toBe(200);

    const row = main.getByRole("listitem").filter({ hasText: `${name} webhook` });
    await expect(row).toContainText("active");
    await row.getByRole("button", { name: "Disable" }).click();
    await expect(row).toContainText("disabled");
    await row.getByRole("button", { name: "Enable" }).click();
    await expect(row).toContainText("active");
    await page.reload();
    await expect(row).toContainText("active");
    // A reload forgets the token: it was shown once.
    await expect(
      main.getByText("Token, shown once - store it where the caller lives:"),
    ).toHaveCount(0);

    await row.getByRole("button", { name: "Rotate the token" }).click();
    await expect(toast(page, "Rotated - the old token died this instant.")).toBeVisible();
    await expect(token).toBeVisible();
    const second = (await token.textContent())!;
    expect(second).not.toBe(first);
    expect(await readFeed(channelId, first)).toBe(401);
    expect(await readFeed(channelId, second)).toBe(200);

    await row.getByRole("button", { name: "Remove the channel" }).click();
    await expect(toast(page, "Channel removed, ledger and all.")).toBeVisible();
    await expect(main.getByText("No doors yet")).toBeVisible();
    expect(await readFeed(channelId, second)).toBe(401);
    await page.reload();
    await expect(main.getByText("No doors yet")).toBeVisible();
  });

  test("removing a channel takes its token off the screen", async ({
    accountPage: page,
    account,
  }) => {
    knownBug(
      "web/app/routes/bots/channels-tab.tsx:84 - remove() reloads the rows but never clears " +
        "`minted`, so 'Token, shown once' keeps showing the removed channel's dead token",
    );
    const { name } = await createBot(account.token, { name: `deadtoken-${stamp()}` });
    await page.goto(`/bots/${name}?tab=channels`);
    const main = page.getByRole("main");
    await main.getByRole("button", { name: "Add webhook" }).click();
    const panel = main.getByText("Token, shown once - store it where the caller lives:");
    await expect(panel).toBeVisible();
    await main.getByRole("button", { name: "Remove the channel" }).click();
    await expect(main.getByText("No doors yet")).toBeVisible();
    await expect(panel).toBeHidden();
  });

  test("a many-chats bot starts a chat and a second one, without a message", async ({
    accountPage: page,
    account,
  }) => {
    const { name } = await createBot(account.token, { name: `talker-${stamp()}` });
    await page.goto(`/bots/${name}`);
    const main = page.getByRole("main");
    await expect(main.getByText(`No conversations with ${name} yet.`)).toBeVisible();

    await main.getByRole("button", { name: "Start one" }).click();
    const message = main.getByRole("textbox", { name: `Message ${name}` });
    await expect(message).toBeVisible();
    await expect(main.getByText("Say what you need.")).toBeVisible();
    await expect(main.getByRole("button", { name: "Send" })).toBeDisabled();
    // One chat needs no list.
    await expect(main.getByText("Chats", { exact: true })).toHaveCount(0);

    await main.getByRole("button", { name: "New chat" }).click();
    await expect(main.getByText("Chats", { exact: true })).toBeVisible();
    // Untitled chats are the rows titled so (the New button shares their accessible name).
    await expect(main.getByTitle("Untitled")).toHaveCount(2);
    await page.reload();
    await expect(main.getByTitle("Untitled")).toHaveCount(2);
    await expect(message).toBeVisible();
  });

  test("a one-chat bot opens straight into its chat", async ({ accountPage: page, account }) => {
    const { name } = await createBot(account.token, { name: `single-${stamp()}` });
    await call(`/bots/${name}`, {
      method: "PATCH",
      token: account.token,
      body: { threads: "one", clear_icon: false, clear_hue: false, clear_title: false },
    });
    await page.goto(`/bots/${name}`);
    const main = page.getByRole("main");
    await expect(main.getByRole("textbox", { name: `Message ${name}` })).toBeVisible();
    await expect(main.getByText(`No conversations with ${name} yet.`)).toHaveCount(0);
    await expect(main.getByRole("button", { name: "New chat" })).toHaveCount(0);
    await page.reload();
    await expect(main.getByRole("textbox", { name: `Message ${name}` })).toBeVisible();
    await expect(main.getByTitle("Untitled")).toHaveCount(0);
  });

  test("a template link that leads nowhere says so, signed out", async ({ page }) => {
    await page.goto("/t/bots/bt_doesnotexist");
    await expect(page.getByText("This template is gone")).toBeVisible();
    await expect(page.getByText(/The link was unpublished, or it never existed/)).toBeVisible();
  });
});
