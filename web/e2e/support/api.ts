/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The API's own doors, for arranging what a flow needs before the browser opens: an account, a
// project, a held task. A flow drives the screen it tests; everything around it is set up here, so
// a test never depends on another test's rows. Nothing here calls a model: a task comes from a
// routine fired with posture "hold", which mints a real record that no worker plans or runs.

export const API_URL = process.env.E2E_API_URL ?? "http://127.0.0.1:8304";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly body: string,
    message: string,
  ) {
    super(message);
  }
}

/** One API call; throws ApiError on any status outside 2xx, naming the body. */
export async function call<T>(
  path: string,
  init: { method?: string; token?: string; body?: unknown } = {},
): Promise<T> {
  const method = init.method ?? (init.body === undefined ? "GET" : "POST");
  for (let attempt = 0; ; attempt++) {
    const response = await fetch(`${API_URL}/v1${path}`, {
      method,
      headers: {
        "content-type": "application/json",
        ...(init.token ? { authorization: `Bearer ${init.token}` } : {}),
      },
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
    });
    const text = await response.text();
    // A stack someone else runs keeps the shipped burst buckets: wait out a 429 rather than fail.
    if (response.status === 429 && attempt < 5) {
      const wait = Number(response.headers.get("retry-after") ?? "2");
      await new Promise((resolve) => setTimeout(resolve, Math.min(wait, 30) * 1000));
      continue;
    }
    if (!response.ok)
      throw new ApiError(response.status, text, `${method} ${path} -> ${response.status}: ${text}`);
    return (text ? JSON.parse(text) : undefined) as T;
  }
}

/** A short suffix that keeps names unique across reruns on the same database. */
export function stamp(): string {
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}

export type Project = { id: string; name: string };

export function createProject(token: string, name: string): Promise<Project> {
  return call<Project>("/projects", { token, body: { name } });
}

export type Routine = { id: string; name: string };

/**
 * A routine whose fires hold: every fire mints a task that waits for a person, so nothing plans or
 * runs. Its When is once a year, so the sweep never fires it during a run.
 */
export function createHeldRoutine(
  token: string,
  init: { name: string; instruction: string },
): Promise<Routine> {
  return call<Routine>("/routines", {
    token,
    body: {
      ...init,
      schedule: { cron: "0 9 1 1 *", timezone: "UTC" },
      posture: "hold",
    },
  });
}

export type HeldTask = { id: string; title: string; goal: string; routine: Routine };

/** A real task record that never runs: a held routine, fired once by hand. */
export async function createHeldTask(token: string, goal: string): Promise<HeldTask> {
  const routine = await createHeldRoutine(token, { name: `Held ${stamp()}`, instruction: goal });
  const fired = await call<{ decision: string; task_id: string | null }>(
    `/routines/${routine.id}/fire`,
    { token, body: {} },
  );
  if (fired.decision !== "held" || !fired.task_id)
    throw new Error(`the held fire did not mint a task: ${JSON.stringify(fired)}`);
  const task = await call<{ title: string | null }>(`/tasks/${fired.task_id}`, { token });
  return { id: fired.task_id, title: task.title ?? goal, goal, routine };
}

/** A held task, cancelled: a settled record whose archive, delete and feedback doors are open. */
export async function createSettledTask(token: string, goal: string): Promise<HeldTask> {
  const task = await createHeldTask(token, goal);
  await call(`/tasks/${task.id}/cancel`, { token, body: {} });
  return task;
}

export type Bot = { name: string };

/** A draft bot with written instructions: creating it asks no model to write them. */
export function createBot(
  token: string,
  init: { name: string; instructions?: string; description?: string },
): Promise<Bot> {
  return call<Bot>("/bots", {
    token,
    body: {
      instructions: "Answer in one short paragraph.",
      can_ask_user: true,
      ...init,
    },
  });
}
