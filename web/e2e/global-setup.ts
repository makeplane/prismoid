/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Seeds what the flows read, through the API's own doors: a fresh account (a new email each run,
// so a rerun on the same database never collides) and one task record. The task comes from a
// routine fired with posture "hold": the fire mints an ordinary task that holds until a person
// sends a direction, so it is a real record that no worker ever plans or runs, and no model is
// called.
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { API_URL, SEED_FILE, call, type Seed } from "./seed";

type Session = { token: string; expires_at: string };
type Fired = { decision: string; task_id: string | null; detail: string };

export default async function globalSetup(): Promise<void> {
  const health = await fetch(`${API_URL}/healthz`).catch(() => null);
  if (!health?.ok) throw new Error(`no API at ${API_URL}: start one (just e2e boots its own)`);

  const stamp = Date.now().toString(36);
  const email = `e2e-${stamp}@example.com`;
  const password = "correct horse battery staple";
  const name = "Ada Tester";
  const session = await call<Session>("/auth/signup", {
    method: "POST",
    body: { email, name, password },
  });
  const token = session.token;

  const goal = "Summarize the tide pool field notes";
  const routine = await call<{ id: string; name: string }>("/routines", {
    method: "POST",
    token,
    body: {
      name: `Tide pools ${stamp}`,
      instruction: goal,
      // A When is required; once a year means the sweep never fires it during a run.
      schedule: { cron: "0 9 1 1 *", timezone: "UTC" },
      posture: "hold",
    },
  });
  const fired = await call<Fired>(`/routines/${routine.id}/fire`, {
    method: "POST",
    token,
    body: {},
  });
  if (fired.decision !== "held" || !fired.task_id) {
    throw new Error(`the held fire did not mint a task: ${JSON.stringify(fired)}`);
  }
  const task = await call<{ title: string | null }>(`/tasks/${fired.task_id}`, { token });

  const seed: Seed = {
    email,
    password,
    name,
    token,
    expiresAt: session.expires_at,
    task: { id: fired.task_id, title: task.title ?? goal, goal },
  };
  mkdirSync(dirname(SEED_FILE), { recursive: true });
  writeFileSync(SEED_FILE, JSON.stringify(seed, null, 2));
}
