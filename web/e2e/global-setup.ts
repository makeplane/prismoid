/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Seeds what the flows share, through the API's own doors: a fresh account (a new email each run,
// so a rerun on the same database never collides) with one held task, and an operator account for
// the console flows. The task comes from a routine fired with posture "hold": the fire mints an
// ordinary task that holds until a person sends a direction, so it is a real record that no worker
// ever plans or runs, and no model is called. The operator scope has no web door outside local
// mode, so it is granted the way an operator grants the first one, `prismoid admin bootstrap`,
// which needs the API's database URL (stack.sh exports it); without it the console flows skip.
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { signIn, signUp } from "./support/accounts";
import { API_URL, createHeldTask } from "./support/api";
import { SEED_FILE, type Seed } from "./seed";

const BACKEND = fileURLToPath(new URL("../../backend", import.meta.url));

function bootstrapAdmin(email: string): boolean {
  if (!process.env.PRISMOID_API_DATABASE_URL) return false;
  const venv = `${BACKEND}/.venv/bin/prismoid`;
  const [command, args] = existsSync(venv)
    ? [venv, ["admin", "bootstrap", "--email", email, "--json"]]
    : ["uv", ["run", "prismoid", "admin", "bootstrap", "--email", email, "--json"]];
  execFileSync(command, args, { cwd: BACKEND, stdio: "pipe" });
  return true;
}

export default async function globalSetup(): Promise<void> {
  const health = await fetch(`${API_URL}/healthz`).catch(() => null);
  if (!health?.ok) throw new Error(`no API at ${API_URL}: start one (just e2e boots its own)`);

  const account = await signUp("e2e");
  const goal = "Summarize the tide pool field notes";
  const task = await createHeldTask(account.token, goal);

  const operator = await signUp("e2e-operator", { name: "Olive Operator" });
  // One console session for the run: the login door counts sign-ins per account per minute.
  const admin = bootstrapAdmin(operator.email)
    ? {
        email: operator.email,
        password: operator.password,
        name: operator.name,
        ...(await signIn(operator)),
      }
    : null;

  const seed: Seed = {
    email: account.email,
    password: account.password,
    name: account.name,
    token: account.token,
    expiresAt: account.expiresAt,
    task: { id: task.id, title: task.title, goal },
    admin,
  };
  mkdirSync(dirname(SEED_FILE), { recursive: true });
  writeFileSync(SEED_FILE, JSON.stringify(seed, null, 2));
}
