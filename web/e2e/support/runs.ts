/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Real runs, for the model flows (e2e/models/) only: each of these asks the stack's model for
// something and spends real money, so no core flow imports this file. A model's words differ run to
// run, so a flow waits on what the server records (a status, a message, a version) and asserts the
// words only where any sane answer must carry them.
import { expect, type Page } from "@playwright/test";
import { call } from "./api";

/** How long one model run may take to settle: seconds on a good day, a minute on a slow provider. */
export const RUN_TIMEOUT = 120_000;

export const TASK_URL = /\/tasks\/([0-9a-f-]{36})$/;

/** The task id in a task page's URL. */
export function taskIdIn(url: string): string {
  const match = new URL(url).pathname.match(TASK_URL);
  if (!match) throw new Error(`not a task page: ${url}`);
  return match[1];
}

type TaskRow = { status: string; turn: number; title: string | null };

/** Start a task through the API: the setup for a flow whose subject comes after the first answer. */
export async function startTask(token: string, goal: string): Promise<string> {
  const accepted = await call<{ task_id: string }>("/tasks", { token, body: { goal } });
  return accepted.task_id;
}

/** Wait until the task's status is one of `want`, and hand back the row. */
export async function waitForTask(
  token: string,
  id: string,
  want: string | string[],
): Promise<TaskRow> {
  const statuses = Array.isArray(want) ? want : [want];
  let row: TaskRow | undefined;
  await expect
    .poll(
      async () => {
        row = await call<TaskRow>(`/tasks/${id}`, { token });
        return row.status;
      },
      { timeout: RUN_TIMEOUT, intervals: [1_000] },
    )
    .toMatch(new RegExp(`^(${statuses.join("|")})$`));
  return row!;
}

/**
 * Answer the gate's questions one by one, as a person would: type into a free-text question, take
 * the first option of a choice. The model writes the questions, so their number and kinds change
 * from run to run (a lone question shows no "1 of 3" counter, and a pick may move on by itself), so
 * the flow follows the panel: while it shows, answer the question it asks and wait for the next.
 * Returns how many questions were answered.
 */
export async function answerQuestions(page: Page): Promise<number> {
  // Only the questions panel offers "Use defaults"; its question is the page's one level-3 heading.
  const panel = page.getByRole("button", { name: "Use defaults" });
  const prompt = page.getByRole("main").getByRole("heading", { level: 3 });
  const answer = page.getByRole("button", { name: /^Answer and (continue|submit)$/ });
  await expect(panel).toBeVisible({ timeout: RUN_TIMEOUT });
  let answered = 0;
  // Read without waiting: the heading goes the moment the last answer sends.
  const question = async () => ((await prompt.count()) ? await prompt.first().textContent() : null);
  while ((await panel.isVisible()) && answered < 10) {
    const asked = await question();
    const movedOn = async () => !(await panel.isVisible()) || (await question()) !== asked;
    const typed = page.getByRole("textbox", { name: "Type your answer…" });
    if (await typed.isVisible()) {
      await typed.fill("Whatever you think is best.");
      await answer.click();
    } else {
      await page.getByRole("button", { name: /^1 / }).click();
      // A lone choice sends itself on the pick; any other waits for the answer button.
      if (!(await settles(movedOn, 3_000))) await answer.click();
    }
    answered++;
    await expect.poll(movedOn).toBe(true);
  }
  await expect(panel).toBeHidden({ timeout: RUN_TIMEOUT });
  return answered;
}

/** Whether `check` comes true within `timeout`: for a screen that may or may not move by itself. */
async function settles(check: () => Promise<boolean>, timeout: number): Promise<boolean> {
  return expect
    .poll(check, { timeout })
    .toBe(true)
    .then(
      () => true,
      () => false,
    );
}
