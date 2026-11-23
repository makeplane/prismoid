/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { expect, test, toast } from "../fixtures";
import { call, createBot, stamp } from "../support/api";
import { RUN_TIMEOUT } from "../support/runs";

// Bots on a real model (the `models` project, E2E_MODELS=1): a reply in the bot's chat, the
// selection evals that gate publishing, and the drafter that writes a bot's instructions.

type Message = { seq: number; role: string; text: string };
type Conversation = { id: string };

/** A bot that knows tides: routing written, so the evals have something to score. */
const ROUTING = {
  job: "Writes tide-table guides for small boat sailors",
  use_when: ["someone wants a written guide to reading a tide table"],
  not_for: ["weather forecasts"],
};

test.describe("a bot on a real model", () => {
  test("answers in its chat, and the conversation is kept", async ({
    accountPage: page,
    account,
  }) => {
    const { name } = await createBot(account.token, {
      name: `tides-${stamp()}`,
      instructions: "You explain tides to small boat sailors in one short paragraph.",
    });
    await page.goto(`/bots/${name}`);
    const main = page.getByRole("main");
    await main.getByRole("button", { name: "Start one" }).click();
    const box = main.getByRole("textbox", { name: `Message ${name}` });
    const send = main.getByRole("button", { name: "Send" });
    await expect(send).toBeDisabled();
    const question = "In one sentence, what is a spring tide?";
    await box.fill(question);
    await send.click();
    await expect(main.getByText(question)).toBeVisible();
    await expect(box).toHaveValue("");

    // The reply streams into the chat; the server's copy of it is what the page must show.
    let reply = "";
    await expect
      .poll(
        async () => {
          const [conversation] = await call<Conversation[]>(`/conversations?bot=${name}`, {
            token: account.token,
          });
          if (!conversation) return "";
          const messages = await call<Message[]>(`/conversations/${conversation.id}/messages`, {
            token: account.token,
          });
          reply = messages.find((m) => m.role === "bot")?.text ?? "";
          return reply;
        },
        { timeout: RUN_TIMEOUT, intervals: [1_000] },
      )
      .not.toBe("");
    // The reply is markdown and the page renders it, so its marks are dropped before matching.
    const opening = reply
      .replace(/[*_`#>[\]]/g, "")
      .split(/\s+/)
      .slice(0, 6)
      .join(" ");
    await expect(main.getByText(opening, { exact: false })).toBeVisible();

    await page.reload();
    await expect(main.getByText(question)).toBeVisible();
    await expect(main.getByText(opening, { exact: false })).toBeVisible();
  });

  test("evals that win the bot's own goal turn it green, and it publishes", async ({
    accountPage: page,
  }) => {
    const name = `guides-${stamp()}`;
    await page.goto("/bots");
    const main = page.getByRole("main");
    await main.getByRole("button", { name: "New bot" }).click();
    const dialog = page.getByRole("dialog", { name: "New bot" });
    await dialog
      .getByRole("textbox", { name: "Instructions (optional)" })
      .fill("You write short, plain guides to reading tide tables for small boat sailors.");
    await dialog.getByRole("textbox", { name: "Name", exact: true }).fill(name);
    await dialog.getByRole("button", { name: "Advanced" }).click();
    await dialog.getByRole("textbox", { name: "What does this bot do?" }).fill(ROUTING.job);
    await dialog
      .getByRole("textbox", { name: "When should it step in?" })
      .fill(ROUTING.use_when[0]);
    await dialog.getByRole("textbox", { name: "What should it refuse?" }).fill(ROUTING.not_for[0]);
    // A goal plainly asking for the bot's work: the Lead plans it, so the eval can seat the bot.
    await dialog
      .getByRole("textbox", { name: "A goal it should win" })
      .fill(
        "Write me a one-page guide to reading the tide table for my harbour before a dinghy sail.",
      );
    await dialog.getByRole("button", { name: "Create bot" }).click();
    await expect(page).toHaveURL(new RegExp(`/bots/${name}\\?tab=configure$`));
    await expect(main.getByText("evals untested", { exact: true })).toBeVisible();
    await expect(main.getByRole("button", { name: /^Publish/ })).toHaveCount(0);

    await main.getByRole("button", { name: "Test evals" }).click();
    await expect(main.getByText("evals green", { exact: true })).toBeVisible({
      timeout: RUN_TIMEOUT,
    });
    await expect(main.getByRole("heading", { name: "Selection evals" })).toBeVisible();
    await expect(main.getByText("1/1", { exact: true })).toBeVisible();

    await main.getByRole("button", { name: "Publish v1" }).click();
    await expect(main.getByText(/^published\s+v1$/)).toBeVisible();
    await expect(main.getByRole("button", { name: /^Publish/ })).toHaveCount(0);
    await page.reload();
    await expect(main.getByText(/^published\s+v1$/)).toBeVisible();
  });

  test("Draft for me writes the instructions as the next draft", async ({
    accountPage: page,
    account,
  }) => {
    // No instructions yet: the drafter writes them from the routing.
    const name = `drafted-${stamp()}`;
    await call("/bots", { token: account.token, body: { name, ...ROUTING, can_ask_user: true } });
    await page.goto(`/bots/${name}?tab=configure`);
    const main = page.getByRole("main");
    const instructions = main.getByRole("textbox", { name: "Bot instructions" });
    await expect(instructions).toHaveValue("");
    await main.getByRole("button", { name: "Draft for me" }).click();
    await expect(
      toast(page, "Writing a fuller draft - it lands here in a moment.").first(),
    ).toBeVisible();

    // The draft lands as the next version. The tab re-reads the bot for 40 s only, and the drafter
    // takes 20 to 40 s or more, so the flow waits on the server's v2 and then reloads.
    test.info().annotations.push({
      type: "bug",
      description:
        "web/app/routes/bots/configure-tab.tsx:76 polls for 40 s after Draft for me (and Test " +
        "evals); a draft that lands later never shows until a reload, though the toast says it " +
        "lands here in a moment",
    });
    await expect
      .poll(
        async () =>
          (await call<{ version: number }>(`/bots/${name}`, { token: account.token })).version,
        { timeout: RUN_TIMEOUT, intervals: [2_000] },
      )
      .toBe(2);
    await page.reload();
    await expect(main.getByRole("listitem").filter({ hasText: /^v2\s*draft/ })).toBeVisible();
    await expect(main.getByRole("textbox", { name: "Bot instructions" })).not.toHaveValue("");
  });
});
