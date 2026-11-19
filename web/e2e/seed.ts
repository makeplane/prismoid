/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// What the flows share: one account and one task record, made through the API before any test
// runs (global-setup.ts) and read back by every spec. Nothing here calls a model.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

export const API_URL = process.env.E2E_API_URL ?? "http://127.0.0.1:8304";
export const SEED_FILE = fileURLToPath(new URL("./.state/seed.json", import.meta.url));

export type Seed = {
  email: string;
  password: string;
  name: string;
  token: string;
  expiresAt: string;
  /** A routine fired with posture "hold": its task exists, is held, and never runs. */
  task: { id: string; title: string; goal: string };
};

export function readSeed(): Seed {
  return JSON.parse(readFileSync(SEED_FILE, "utf8")) as Seed;
}

/** One API call with the seed's bearer; throws on any status outside 2xx, naming the body. */
export async function call<T>(
  path: string,
  init: { method?: string; token?: string; body?: unknown } = {},
): Promise<T> {
  const response = await fetch(`${API_URL}/v1${path}`, {
    method: init.method ?? "GET",
    headers: {
      "content-type": "application/json",
      ...(init.token ? { authorization: `Bearer ${init.token}` } : {}),
    },
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
  });
  const text = await response.text();
  if (!response.ok)
    throw new Error(`${init.method ?? "GET"} ${path} -> ${response.status}: ${text}`);
  return (text ? JSON.parse(text) : undefined) as T;
}
