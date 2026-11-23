/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { Page } from "@playwright/test";
import { chooseOption, composer, escapeRegExp, expect, knownBug, test } from "../fixtures";
import { call } from "../support/api";
import {
  answerQuestions,
  RUN_TIMEOUT,
  startTask,
  TASK_URL,
  taskIdIn,
  waitForTask,
} from "../support/runs";

// Tasks that run on a real model (the `models` project, E2E_MODELS=1): a question, a follow-up, a
// fork, a Markdown document with its verification, and a round of clarifying questions. Each one is
// small enough to settle in seconds on paper, so none of them boots a Machine; the document's
// verification runs in a throwaway frame, which needs the compose frame network (stack.sh checks).

function messages(page: Page) {
  return page.getByRole("region", { name: "Messages" });
}

/** The page header's status icon: its name carries the task's status word. */
function status(page: Page, word: string) {
  return page.getByRole("main").getByRole("img", { name: `Status: ${word}`, exact: true });
}

/** One finished turn on the thread: its step summary is a button that opens the step's detail. */
function turns(page: Page) {
  return messages(page).getByRole("button", { name: /^Completed \d+ steps? / });
}

test.describe("a task on a real model", () => {
  test("a question sent from home comes back answered and is kept", async ({
    accountPage: page,
  }) => {
    const goal = "What is the capital of France? Answer in one short sentence.";
    await page.goto("/");
    await composer(page).fill(goal);
    await page.getByRole("button", { name: "Submit" }).click();
    await expect(page).toHaveURL(TASK_URL);

    await expect(status(page, "delivered")).toBeVisible({ timeout: RUN_TIMEOUT });
    await expect(messages(page)).toContainText(goal);
    await expect(messages(page)).toContainText("Paris");
    await expect(turns(page)).toHaveCount(1);
    // The header's spend comes from the ledger: what the run cost, in how many model calls.
    await expect(
      page.getByRole("main").getByText(/^\$\d+\.\d+ · [1-9]\d* model calls?$/),
    ).toBeVisible();

    // The record replays the same thread after a reload.
    await page.reload();
    await expect(status(page, "delivered")).toBeVisible();
    await expect(messages(page)).toContainText("Paris");

    // Delivery knocks the inbox, and the item opens on the answer.
    await page.goto("/inbox");
    const item = page
      .getByRole("main")
      .getByRole("button", { name: new RegExp(`Delivered: ${escapeRegExp(goal)}`) });
    await item.first().click();
    await expect(page.getByRole("main").getByRole("heading", { level: 2 })).toContainText(goal);
    await expect(page.getByRole("main")).toContainText("Paris");
  });

  test("a follow-up continues the same task as its next turn", async ({
    accountPage: page,
    account,
  }) => {
    const id = await startTask(account.token, "Name one planet in the solar system. One word.");
    await waitForTask(account.token, id, "delivered");
    await page.goto(`/tasks/${id}`);
    await expect(turns(page)).toHaveCount(1);

    const ask = "Now name a different planet. One word.";
    await page.getByRole("textbox", { name: "Ask a follow-up…" }).fill(ask);
    await page.getByRole("button", { name: "Submit" }).click();
    await expect(messages(page)).toContainText(ask);
    await expect(turns(page)).toHaveCount(2, { timeout: RUN_TIMEOUT });
    await expect(status(page, "delivered")).toBeVisible();
    // A follow-up is never a new task: the same page, the same row, its turn counter moved on.
    expect(taskIdIn(page.url())).toBe(id);
    expect((await waitForTask(account.token, id, "delivered")).turn).toBe(2);
  });

  test("a fork branches a delivered task, held until a direction launches it", async ({
    accountPage: page,
    account,
  }) => {
    const id = await startTask(account.token, "Name one planet in the solar system. One word.");
    await waitForTask(account.token, id, "delivered");
    await page.goto(`/tasks/${id}`);
    await page.getByRole("button", { name: "Task actions", exact: true }).click();
    await page
      .getByRole("menu", { name: "Task actions" })
      .getByRole("menuitem", { name: "Fork this task" })
      .click();

    // The branch is a new task carrying the whole conversation, born held.
    await expect(page).not.toHaveURL(new RegExp(id));
    await expect(page).toHaveURL(TASK_URL);
    const branch = taskIdIn(page.url());
    await expect(page.getByRole("navigation", { name: "Breadcrumb" })).toContainText("[Forked]");
    await expect(messages(page).getByRole("status")).toHaveText(
      "Held. This branch has not started. Send a direction below to launch it.",
    );
    await expect(turns(page)).toHaveCount(1);

    await page
      .getByRole("textbox", { name: "Where should this branch go?" })
      .fill("Now name the largest planet. One word.");
    await page.getByRole("button", { name: "Submit" }).click();
    await expect(status(page, "delivered")).toBeVisible({ timeout: RUN_TIMEOUT });
    await expect(messages(page)).toContainText("Jupiter");

    // The parent keeps its own thread: one turn, still delivered.
    const parent = await waitForTask(account.token, id, "delivered");
    expect(parent.turn).toBe(1);
    expect(
      (await call<{ status: string }>(`/tasks/${branch}`, { token: account.token })).status,
    ).toBe("delivered");
  });

  test("a Markdown document is published, opened in the reader and verified", async ({
    accountPage: page,
  }) => {
    await page.goto("/");
    await chooseOption(page, "Deliverable", "Document · Markdown");
    await composer(page).fill(
      "Write a three-sentence note on why tide tables matter to small boat sailors.",
    );
    await page.getByRole("button", { name: "Submit" }).click();
    await expect(page).toHaveURL(TASK_URL);

    // The published document opens in the reader by itself.
    const reader = page.getByRole("region", { name: "Artifact reader" });
    await expect(reader.getByRole("heading", { level: 1 })).toBeVisible({ timeout: RUN_TIMEOUT });
    await expect(reader).toContainText(/tide/i);
    await expect(status(page, "delivered")).toBeVisible({ timeout: RUN_TIMEOUT });
    await expect(messages(page).getByRole("button", { name: /Document/ })).toBeVisible();

    // The file downloads as Markdown.
    const download = page.waitForEvent("download");
    await reader.getByRole("button", { name: "Download" }).click();
    expect((await download).suggestedFilename()).toMatch(/\.md$/);

    // Verification signs after delivery, on its own queue: the thread shows the receipt when it
    // lands. An unsourced note may well come back unverified, so the verdict word is not asserted.
    await expect(messages(page).getByText(/ · document-suite · judged by /)).toBeVisible({
      timeout: RUN_TIMEOUT,
    });
    await expect(messages(page).getByRole("button", { name: "See the receipt" })).toBeVisible();
  });

  test("the open reader shows the receipt once the check finishes", async ({
    accountPage: page,
  }) => {
    knownBug(
      "web/app/components/artifact/artifact-view.tsx:127-167 fetches the verification once, when " +
        "the reader opens (its effect depends on currentId only); the reader opens at publish, " +
        "before the check signs, so it keeps 'Not verified yet' after the thread shows the receipt",
    );
    await page.goto("/");
    await chooseOption(page, "Deliverable", "Document · Markdown");
    await composer(page).fill("Write two sentences on what a neap tide is.");
    await page.getByRole("button", { name: "Submit" }).click();
    const reader = page.getByRole("region", { name: "Artifact reader" });
    await expect(reader.getByRole("heading", { level: 1 })).toBeVisible({ timeout: RUN_TIMEOUT });
    await expect(messages(page).getByText(/ · document-suite · judged by /)).toBeVisible({
      timeout: RUN_TIMEOUT,
    });
    await expect(
      reader.getByText("Not verified yet. The receipt shows here once the check finishes."),
    ).toBeHidden();
  });

  test("clarifying questions open a gate the person answers, then the task delivers", async ({
    accountPage: page,
    account,
  }) => {
    await page.goto("/");
    await chooseOption(page, "Clarifying questions", "Always");
    await composer(page).fill("Recommend one book for me to read next. Answer in one sentence.");
    await page.getByRole("button", { name: "Submit" }).click();
    await expect(page).toHaveURL(TASK_URL);
    const id = taskIdIn(page.url());

    // One numbered batch, before any work starts: the header says the task needs its person.
    await expect(page.getByRole("main").getByText(/^Needs you\b/)).toBeVisible({
      timeout: RUN_TIMEOUT,
    });
    await expect(messages(page)).toContainText("Waiting for your answer");
    const gates = await call<{ kind: string; status: string }[]>(`/gates?task_id=${id}`, {
      token: account.token,
    });
    expect(gates.map((g) => g.kind)).toEqual(["input"]);

    expect(await answerQuestions(page)).toBeGreaterThan(0);
    await expect(status(page, "delivered")).toBeVisible({ timeout: RUN_TIMEOUT });
    await expect(turns(page).last()).toBeVisible();
  });
});
