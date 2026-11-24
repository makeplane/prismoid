/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// What the flows share: one account with one held task, made through the API before any test runs
// (global-setup.ts) and read back by every spec, plus the operator account the console flows sign in
// with when the stack let global-setup make one. Nothing here calls a model.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

/** E2E_STATE_DIR moves the run's state, so two runs against one stack never share a seed file. */
const STATE_DIR = process.env.E2E_STATE_DIR ?? fileURLToPath(new URL("./.state", import.meta.url));
export const SEED_FILE = `${STATE_DIR}/seed.json`;

export type Seed = {
  email: string;
  password: string;
  name: string;
  token: string;
  expiresAt: string;
  /** A routine fired with posture "hold": its task exists, is held, and never runs. */
  task: { id: string; title: string; goal: string };
  /** An account holding the operator scope, or null when this process cannot grant one. */
  admin: { email: string; password: string; name: string; token: string; expiresAt: string } | null;
};

export function readSeed(): Seed {
  return JSON.parse(readFileSync(SEED_FILE, "utf8")) as Seed;
}
