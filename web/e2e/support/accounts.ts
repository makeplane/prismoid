/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// People, made through the API's own doors. A flow that changes an account (a reset revokes every
// session, a workspace switch rebinds one, a rename shows up everywhere) signs up its own, so no
// flow can break another and a rerun on the same database never collides.
import type { Browser, Page } from "@playwright/test";
import { call, stamp } from "./api";
import { codeIn, waitForMail } from "./mailbox";

export const PASSWORD = "correct horse battery staple";

// The keys the app reads its session from (app/lib/storage.ts).
const TOKEN_KEY = "prismoid.token";
const EXPIRES_KEY = "prismoid.expires";

export type Account = {
  id: string;
  email: string;
  password: string;
  name: string;
  token: string;
  expiresAt: string;
  workspace: { id: string; name: string };
};

type Session = { token: string; expires_at: string; user: { id: string } };
type Me = { workspace: { id: string; name: string } | null };

/** A new person with a personal workspace; `label` becomes part of the email, for a readable trace. */
export async function signUp(
  label = "e2e",
  init: { name?: string; inviteCode?: string } = {},
): Promise<Account> {
  const email = `${label}-${stamp()}@example.com`;
  const name = init.name ?? "Ada Tester";
  const session = await call<Session>("/auth/signup", {
    body: {
      email,
      name,
      password: PASSWORD,
      ...(init.inviteCode ? { invite_code: init.inviteCode } : {}),
    },
  });
  const me = await call<Me>("/auth/me", { token: session.token });
  if (!me.workspace) throw new Error(`${email} signed up without a workspace`);
  return {
    id: session.user.id,
    email,
    password: PASSWORD,
    name,
    token: session.token,
    expiresAt: session.expires_at,
    workspace: me.workspace,
  };
}

/** A fresh session for an existing account: a second device, say, or the one a reset must revoke. */
export async function signIn(who: {
  email: string;
  password: string;
}): Promise<{ token: string; expiresAt: string }> {
  const session = await call<Session>("/auth/login", {
    body: { email: who.email, password: who.password },
  });
  return { token: session.token, expiresAt: session.expires_at };
}

/** Prove the account's address with the code signup emailed: only a proven address sends invites. */
export async function verifyEmail(account: Account): Promise<void> {
  const mail = await waitForMail(account.email, /Verify your email/);
  await call("/auth/me/verify-email", {
    token: account.token,
    body: { code: codeIn(mail, "ev_") },
  });
}

/**
 * Bring `member` into `owner`'s workspace: the owner invites by email (proving their address first
 * if it is not yet, since only a proven address sends invites), and the member trades the emailed
 * code. Project members must belong to the project's workspace first.
 */
export async function joinWorkspace(
  owner: Account,
  member: Account,
  role: "member" | "admin" | "guest" = "member",
): Promise<void> {
  const me = await call<{ email_verified: boolean }>("/auth/me", { token: owner.token });
  if (!me.email_verified) await verifyEmail(owner);
  await call(`/workspaces/${owner.workspace.id}/invites`, {
    token: owner.token,
    body: { email: member.email, role },
  });
  const mail = await waitForMail(member.email, /invited/i);
  await call("/workspaces/invites/accept", {
    token: member.token,
    body: { code: codeIn(mail, "oi_") },
  });
}

/** Put the account's session where the app looks for it, before any page script runs. */
export async function useSession(page: Page, account: Pick<Account, "token" | "expiresAt">) {
  await page.addInitScript(
    ([tokenKey, expiresKey, token, expiresAt]) => {
      localStorage.setItem(tokenKey, token);
      localStorage.setItem(expiresKey, expiresAt);
    },
    [TOKEN_KEY, EXPIRES_KEY, account.token, account.expiresAt] as const,
  );
}

/** A second browser, signed in as someone else: the other side of a share, an invite or a join. */
export async function pageFor(browser: Browser, account: Account, baseURL: string): Promise<Page> {
  const context = await browser.newContext({ baseURL });
  const page = await context.newPage();
  await useSession(page, account);
  return page;
}
