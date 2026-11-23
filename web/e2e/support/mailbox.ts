/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The stack's outbox. In dev the API's console mail backend prints every email to its stdout in a
// box (prismoid/mailer.py), and stack.sh writes that stdout to E2E_API_LOG; a flow that needs an
// emailed code (a password reset, an address proof, a workspace invite) reads it from there. On a
// stack whose log this process cannot read, those flows skip themselves (`mailAvailable`).
import { existsSync, readFileSync } from "node:fs";

const LOG = process.env.E2E_API_LOG ?? "";

export type Mail = { to: string; subject: string; body: string };

export function mailAvailable(): boolean {
  return LOG !== "" && existsSync(LOG);
}

/** Every console email in the log, oldest first. */
function outbox(): Mail[] {
  const mails: Mail[] = [];
  let current: string[] | null = null;
  for (const line of readFileSync(LOG, "utf8").split("\n")) {
    if (line.startsWith("┌─") && line.includes("outbound email")) current = [];
    else if (line.startsWith("└─") && current) {
      const to =
        current
          .find((l) => l.startsWith("To:"))
          ?.slice(3)
          .trim() ?? "";
      const subject =
        current
          .find((l) => l.startsWith("Subject:"))
          ?.slice(8)
          .trim() ?? "";
      mails.push({ to, subject, body: current.join("\n") });
      current = null;
    } else if (current && line.startsWith("│")) current.push(line.slice(1).trim());
  }
  return mails;
}

/**
 * The newest email to `to` whose subject matches, waiting for it to land: the API sends after its
 * commit, from a background task, so the email trails the response by a moment.
 */
export async function waitForMail(
  to: string,
  subject: RegExp,
  { after = 0, timeoutMs = 15_000 }: { after?: number; timeoutMs?: number } = {},
): Promise<Mail> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const matching = outbox().filter(
      (m) => m.to.toLowerCase() === to.toLowerCase() && subject.test(m.subject),
    );
    if (matching.length > after) return matching[matching.length - 1];
    if (Date.now() > deadline) throw new Error(`no email to ${to} matching ${subject}`);
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
}

/** How many emails to `to` match so far: pass it as `after` to wait for the next one. */
export function mailCount(to: string, subject: RegExp): number {
  if (!mailAvailable()) return 0;
  return outbox().filter((m) => m.to.toLowerCase() === to.toLowerCase() && subject.test(m.subject))
    .length;
}

/** The first token with this prefix in the email (`cr_`, `ev_`, `oi_`). */
export function codeIn(mail: Mail, prefix: string): string {
  const match = mail.body.match(new RegExp(`${prefix}[A-Za-z0-9_-]+`));
  if (!match) throw new Error(`no ${prefix} code in the email "${mail.subject}"`);
  return match[0];
}
