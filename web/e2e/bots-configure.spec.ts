/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { expect, knownBug, test, toast } from "./fixtures";
import { createBot, stamp } from "./support/api";

// Bots: the messenger's list and the Configure tab. Every bot here is a draft with written
// instructions, so nothing asks a model to draft them, and nothing tests evals or publishes.
test.describe("bots: create and configure", () => {
  test("creates a draft bot from the empty list", async ({ accountPage: page }) => {
    const name = `desk-${stamp()}`;
    await page.goto("/bots");
    const main = page.getByRole("main");
    await expect(main.getByText("No bots yet")).toBeVisible();
    await expect(main.getByText("0 bots · 0 pinned to sidebar")).toBeVisible();
    await expect(main.getByText("Pick a bot")).toBeVisible();

    await main.getByRole("button", { name: "New bot" }).click();
    const dialog = page.getByRole("dialog", { name: "New bot" });
    const create = dialog.getByRole("button", { name: "Create bot" });
    await expect(create).toBeDisabled();
    // Instructions go in before the name: with them written, the offer to have a model draft
    // them is gone, so Create can never start the drafter.
    await dialog
      .getByRole("textbox", { name: "Instructions (optional)" })
      .fill("Review vendor terms in one short paragraph.");
    await expect(
      dialog.getByRole("checkbox", { name: "Write instructions for me after creating" }),
    ).toHaveCount(0);
    await dialog.getByRole("textbox", { name: "Name", exact: true }).fill(name);
    await expect(create).toBeEnabled();

    await dialog.getByRole("button", { name: "Advanced" }).click();
    await dialog
      .getByRole("textbox", { name: "What does this bot do?" })
      .fill("Reviews vendor contracts");
    await dialog
      .getByRole("textbox", { name: "When should it step in?" })
      .fill("a vendor sends new terms");
    await dialog.getByRole("textbox", { name: "What should it refuse?" }).fill("legal advice");
    await expect(dialog.getByRole("textbox", { name: "A goal it should win" })).toBeVisible();
    await create.click();

    await expect(toast(page, `${name} created as a draft.`)).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`/bots/${name}\\?tab=configure$`));
    await expect(main.getByRole("heading", { name, level: 2 })).toBeVisible();
    await expect(main.getByText(/^draft\s+v1$/)).toBeVisible();
    await expect(main.getByText("evals untested", { exact: true })).toBeVisible();
    await expect(main.getByText("1 bot · 0 pinned to sidebar")).toBeVisible();
    // The routing triple composes the sentence the selector reads; the header shows its job.
    await expect(main.getByText("Reviews vendor contracts.", { exact: true })).toBeVisible();
    await expect(main.getByRole("textbox", { name: "What this bot does" })).toHaveValue(
      "Reviews vendor contracts",
    );
    await expect(
      main.getByText(/Routes as: .Reviews vendor contracts\. Load when a vendor sends new terms\./),
    ).toBeVisible();
    // Publishing waits for green evals: the evals are offered, Publish is not.
    await expect(main.getByRole("button", { name: "Test evals" })).toBeVisible();
    await expect(main.getByRole("button", { name: /^Publish/ })).toHaveCount(0);

    await page.reload();
    await expect(main.getByRole("heading", { name, level: 2 })).toBeVisible();
    await expect(main.getByRole("textbox", { name: "Bot instructions" })).toHaveValue(
      "Review vendor terms in one short paragraph.",
    );
  });

  test("refuses a reserved name and a name that names a model", async ({ accountPage: page }) => {
    await page.goto("/bots");
    const main = page.getByRole("main");
    await main.getByRole("button", { name: "New bot" }).click();
    const dialog = page.getByRole("dialog", { name: "New bot" });
    await dialog.getByRole("textbox", { name: "Instructions (optional)" }).fill("Answer briefly.");
    const nameBox = dialog.getByRole("textbox", { name: "Name", exact: true });
    for (const [typed, reason] of [
      ["assistant", /'assistant' is reserved for Prismoid's own agents/],
      ["claude-helper", /'claude-helper' names a model\. A bot is a role/],
    ] as const) {
      await nameBox.fill(typed);
      await dialog.getByRole("button", { name: "Create bot" }).click();
      await expect(page.getByRole("alert").filter({ hasText: reason })).toBeVisible();
      // The dialog stays open with what was typed, so the person can fix the name in place.
      await expect(nameBox).toHaveValue(typed);
    }
    await dialog.getByRole("button", { name: "Close" }).click();
    await expect(main.getByText("No bots yet")).toBeVisible();
    await page.reload();
    await expect(main.getByText("0 bots · 0 pinned to sidebar")).toBeVisible();
  });

  test("saves the title and the work dials, and keeps them", async ({
    accountPage: page,
    account,
  }) => {
    const { name } = await createBot(account.token, { name: `dials-${stamp()}` });
    await page.goto(`/bots/${name}?tab=configure`);
    const main = page.getByRole("main");

    await main.getByRole("textbox", { name: "Bot title" }).fill("Contracts desk");
    const save = main.getByRole("button", { name: "Save", exact: true });
    await save.click();
    await expect(toast(page, "Title saved.")).toBeVisible();
    // Saved means clean: the Save goes away, so the work dials' Save below is the only one.
    await expect(save).toHaveCount(0);
    await expect(main.getByText("Contracts desk", { exact: true })).toBeVisible();

    const ask = main.getByRole("radio", { name: /^Ask/ });
    const oneChat = main.getByRole("radio", { name: /^One chat/ });
    const cap = main.getByRole("textbox", { name: "Cap per task in dollars" });
    await expect(main.getByRole("radio", { name: /^Auto/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    await expect(main.getByRole("radio", { name: /^Many chats/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    await ask.click();
    await oneChat.click();
    await cap.fill("five");
    await save.click();
    await expect(
      page.getByRole("alert").filter({ hasText: "The cap is dollars per task - a number." }),
    ).toBeVisible();
    await cap.fill("7.5");
    await save.click();
    await expect(toast(page, "Work dials saved.")).toBeVisible();
    await expect(cap).toHaveValue("7.50");
    await expect(save).toHaveCount(0);

    await page.reload();
    await expect(main.getByRole("textbox", { name: "Bot title" })).toHaveValue("Contracts desk");
    await expect(ask).toHaveAttribute("aria-checked", "true");
    await expect(oneChat).toHaveAttribute("aria-checked", "true");
    await expect(cap).toHaveValue("7.50");
  });

  test("saving the instructions mints draft v2", async ({ accountPage: page, account }) => {
    const { name } = await createBot(account.token, { name: `versions-${stamp()}` });
    await page.goto(`/bots/${name}?tab=configure`);
    const main = page.getByRole("main");
    await expect(main.getByText(/^draft\s+v1$/)).toBeVisible();

    const instructions = main.getByRole("textbox", { name: "Bot instructions" });
    await instructions.fill("Answer in two short sentences, plainly.");
    await main.getByRole("button", { name: "Save as draft v2" }).click();
    await expect(toast(page, "Saved as draft v2.")).toBeVisible();
    await expect(main.getByText(/^draft\s+v2$/)).toBeVisible();
    await expect(main.getByRole("button", { name: /^Save as draft/ })).toHaveCount(0);
    await expect(main.getByRole("listitem").filter({ hasText: /^v2/ })).toBeVisible();
    await expect(main.getByRole("button", { name: /^Publish/ })).toHaveCount(0);

    await page.reload();
    await expect(main.getByText(/^draft\s+v2$/)).toBeVisible();
    await expect(instructions).toHaveValue("Answer in two short sentences, plainly.");
  });

  test("pins a bot to the sidebar and unpins it", async ({ accountPage: page, account }) => {
    const { name } = await createBot(account.token, { name: `pinned-${stamp()}` });
    await page.goto(`/bots/${name}`);
    const main = page.getByRole("main");
    // The sidebar's link is named by the bot alone; the list row's link also names its pin.
    const sidebarLink = page.getByRole("link", { name, exact: true });
    await expect(sidebarLink).toHaveCount(0);

    await main.getByRole("button", { name: `Pin ${name} to the sidebar` }).click();
    await expect(sidebarLink).toBeVisible();
    await expect(main.getByText("1 bot · 1 pinned to sidebar")).toBeVisible();
    await page.reload();
    await expect(sidebarLink).toBeVisible();

    await main.getByRole("button", { name: `Unpin ${name} from the sidebar` }).click();
    await expect(sidebarLink).toHaveCount(0);
    await expect(main.getByText("1 bot · 0 pinned to sidebar")).toBeVisible();
    await page.reload();
    await expect(main.getByRole("button", { name: `Pin ${name} to the sidebar` })).toBeVisible();
    await expect(sidebarLink).toHaveCount(0);
  });

  test("changes the bot's face and keeps it", async ({ accountPage: page, account }) => {
    const { name } = await createBot(account.token, { name: `face-${stamp()}` });
    await page.goto(`/bots/${name}`);
    const face = page.getByRole("button", { name: "Change this bot's face" });
    // Toasts are dialogs too: the picker is the one holding the colour row.
    const picker = page
      .getByRole("dialog")
      .filter({ has: page.getByRole("radiogroup", { name: "Icon color" }) });
    const patched = () =>
      page.waitForResponse(
        (r) => r.url().includes(`/v1/bots/${name}`) && r.request().method() === "PATCH" && r.ok(),
      );

    await face.click();
    const rocket = picker.getByRole("button", { name: "rocket", exact: true });
    await expect(rocket).toHaveAttribute("aria-pressed", "false");
    await Promise.all([patched(), rocket.click()]);
    await expect(rocket).toHaveAttribute("aria-pressed", "true");
    const hue = picker.getByRole("radio", { name: "Color 3" });
    await Promise.all([patched(), hue.click()]);
    await expect(hue).toHaveAttribute("aria-checked", "true");

    await page.reload();
    await face.click();
    await expect(rocket).toHaveAttribute("aria-pressed", "true");
    await expect(hue).toHaveAttribute("aria-checked", "true");
    await expect(picker.getByRole("button", { name: "Reset" })).toBeVisible();
  });

  test("search narrows the list", async ({ accountPage: page, account }) => {
    const tag = stamp();
    await createBot(account.token, { name: `alpha-${tag}` });
    await createBot(account.token, { name: `beta-${tag}` });
    await page.goto("/bots");
    const main = page.getByRole("main");
    await expect(main.getByText("2 bots · 0 pinned to sidebar")).toBeVisible();

    const search = main.getByRole("textbox", { name: "Search bots…" });
    await search.fill("beta");
    await expect(main.getByRole("link", { name: new RegExp(`^beta-${tag}`) })).toBeVisible();
    await expect(main.getByRole("link", { name: new RegExp(`^alpha-${tag}`) })).toHaveCount(0);
    await expect(main.getByText("1 bot · 0 pinned to sidebar")).toBeVisible();

    await search.fill("no-such-bot");
    await expect(main.getByText("No matches for “no-such-bot”")).toBeVisible();
    await search.fill("");
    await expect(main.getByText("2 bots · 0 pinned to sidebar")).toBeVisible();
  });

  test("a search that hides the open bot keeps its pane and the unsaved edit", async ({
    accountPage: page,
    account,
  }) => {
    knownBug(
      "web/app/routes/bots/route.tsx:150 - the open bot is looked up in the search-filtered rows, " +
        "so a search that excludes it swaps the pane for 'Pick a bot' and clearing the search " +
        "remounts the pane, discarding unsaved Configure edits",
    );
    const tag = stamp();
    const { name } = await createBot(account.token, { name: `open-${tag}` });
    await createBot(account.token, { name: `other-${tag}` });
    await page.goto(`/bots/${name}?tab=configure`);
    const main = page.getByRole("main");
    const instructions = main.getByRole("textbox", { name: "Bot instructions" });
    await instructions.fill("Unsaved edit");

    const search = main.getByRole("textbox", { name: "Search bots…" });
    await search.fill("other");
    await expect(main.getByRole("link", { name: new RegExp(`^other-${tag}`) })).toBeVisible();
    await expect(main.getByRole("link", { name: new RegExp(`^open-${tag}`) })).toHaveCount(0);
    await search.fill("");
    await expect(main.getByRole("link", { name: new RegExp(`^open-${tag}`) })).toBeVisible();
    await expect(instructions).toHaveValue("Unsaved edit");
  });
});
