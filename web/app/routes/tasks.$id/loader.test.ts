/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The task page's loader is where the canonical URL is enforced: the page module
// serves BOTH /tasks/:id and /projects/:projectId/tasks/:id, and the second a task is read, its own
// project_id decides which one is right. The segment in the URL never authorizes - the GET is by
// task id alone - so a wrong or garbage project segment simply redirects.
//
// HOW it redirects matters (fix round 1): REPLACE when the bar already shows the wrong URL (a
// fresh load, Back/Forward, a revalidation), PUSH when the reader clicked here from elsewhere -
// a replace there would overwrite the page they came from and Back would skip it.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("~/lib/api/client", async (orig) => ({
  ...(await orig<typeof import("~/lib/api/client")>()),
  api: { GET: state.get },
  unwrap: async (p: Promise<{ err?: Error }>) => {
    const v = await p;
    if (v?.err) throw v.err;
    return v;
  },
}));

import { forgetProjectName, knownProjectName, rememberProjectName } from "~/lib/project-names";
import { clientLoader, handle } from "./route";

const S = "5a1d8c52-0f4e-4a54-9d0b-0a3b1d8d2f10";
const T = "62b53b15-c44c-455f-9c78-f752e2bf132e";
const OTHER = "9c9a7a0e-5bd1-45d0-8d15-3c1b8b6f1a77";

/** What api.GET resolves, by path: the task row, and the Project (best-effort). */
function wire({
  task,
  project,
}: {
  task: Record<string, unknown>;
  project?: Record<string, unknown> | Error;
}) {
  state.get.mockImplementation(async (path: string) => {
    if (path === "/v1/tasks/{task_id}") return task;
    if (path === "/v1/projects/{project_id}") {
      if (project instanceof Error) return { err: project };
      return project ?? { err: new Error("not found") };
    }
    throw new Error(`unexpected GET ${path}`);
  });
}

const projectReads = () =>
  state.get.mock.calls.filter(([p]) => p === "/v1/projects/{project_id}").length;

/** The address bar when the loader runs: a fresh load or revalidation shows the URL being
 *  loaded; an in-app click still shows the page it came from. */
const bar = (pathname: string, hash = "") =>
  vi.stubGlobal("window", { location: { pathname, hash } });

const run = (url: string, params: Record<string, string>) =>
  (clientLoader as unknown as (a: unknown) => Promise<unknown>)({
    request: new Request(`http://app.test${url}`),
    params,
  });

/** The loader throws a redirect Response; hand back where it points and whether it replaces. */
async function redirected(url: string, params: Record<string, string>) {
  try {
    await run(url, params);
  } catch (e) {
    if (e instanceof Response) {
      return {
        status: e.status,
        to: e.headers.get("Location"),
        replace: e.headers.get("X-Remix-Replace") === "true",
      };
    }
    throw e;
  }
  return null;
}

beforeEach(() => {
  state.get.mockReset();
  for (const id of [S, OTHER]) forgetProjectName(id);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the task page loader - the canonical URL", () => {
  it("/tasks/<id> on a Project task redirects (replace) to the Project's URL, keeping the query", async () => {
    wire({ task: { id: T, project_id: S, title: "Memo" } });
    bar(`/tasks/${T}`);
    expect(await redirected(`/tasks/${T}?turn=2`, { id: T })).toEqual({
      status: 302,
      to: `/projects/${S}/tasks/${T}?turn=2`,
      replace: true,
    });
  });

  it("keeps the #fragment on a fresh load, where the bar still holds this very address", async () => {
    wire({ task: { id: T, project_id: S, title: "Memo" } });
    bar(`/tasks/${T}`, "#msg-7");
    expect((await redirected(`/tasks/${T}`, { id: T }))?.to).toBe(
      `/projects/${S}/tasks/${T}#msg-7`,
    );
  });

  it("the nested URL with the wrong Project (moved, or garbage) redirects to the task's own Project", async () => {
    wire({ task: { id: T, project_id: S, title: "Memo" } });
    bar(`/projects/${OTHER}/tasks/${T}`);
    expect(await redirected(`/projects/${OTHER}/tasks/${T}`, { projectId: OTHER, id: T })).toEqual({
      status: 302,
      to: `/projects/${S}/tasks/${T}`,
      replace: true,
    });
  });

  it("the nested URL on a task moved to personal redirects to /tasks/<id>", async () => {
    wire({ task: { id: T, project_id: null, title: "Memo" } });
    bar(`/projects/${S}/tasks/${T}`);
    expect(await redirected(`/projects/${S}/tasks/${T}`, { projectId: S, id: T })).toEqual({
      status: 302,
      to: `/tasks/${T}`,
      replace: true,
    });
  });

  it("an in-app click from another page PUSHES: history stays [origin, canonical]", async () => {
    wire({ task: { id: T, project_id: S, title: "Memo" } });
    bar("/bots/foo", "#unrelated");
    expect(await redirected(`/tasks/${T}?turn=2`, { id: T })).toEqual({
      status: 302,
      to: `/projects/${S}/tasks/${T}?turn=2`, // the origin's #unrelated is not this navigation's
      replace: false,
    });
  });

  it("the server authorizes by task id: the task is read without the URL's project", async () => {
    wire({ task: { id: T, project_id: S, title: "Memo" }, project: { id: S, name: "Acme" } });
    bar(`/projects/${S}/tasks/${T}`);
    await run(`/projects/${S}/tasks/${T}`, { projectId: S, id: T });
    const taskCall = state.get.mock.calls.find(([p]) => p === "/v1/tasks/{task_id}");
    expect(taskCall?.[1]).toEqual({ params: { path: { task_id: T } } });
  });

  it("a personal task at /tasks/<id> stays: no redirect, no Project", async () => {
    wire({ task: { id: T, project_id: null, title: "Memo" } });
    bar(`/tasks/${T}`);
    const data = (await run(`/tasks/${T}`, { id: T })) as {
      detail: { id: string };
      project: unknown;
    };
    expect(data.detail.id).toBe(T);
    expect(data.project).toBeNull();
    expect(projectReads()).toBe(0);
  });

  it("the canonical nested URL stays, and carries the Project's name for the crumb", async () => {
    wire({ task: { id: T, project_id: S, title: "Memo" }, project: { id: S, name: "Acme" } });
    bar(`/projects/${S}/tasks/${T}`);
    const data = (await run(`/projects/${S}/tasks/${T}`, { projectId: S, id: T })) as {
      detail: { id: string };
      project: { id: string; name: string } | null;
    };
    expect(data.detail.id).toBe(T);
    expect(data.project).toEqual({ id: S, name: "Acme" });
  });

  it("an unreadable Project degrades the crumb, never the page", async () => {
    wire({ task: { id: T, project_id: S, title: "Memo" }, project: new Error("403") });
    bar(`/projects/${S}/tasks/${T}`);
    const data = (await run(`/projects/${S}/tasks/${T}`, { projectId: S, id: T })) as {
      project: unknown;
    };
    expect(data.project).toBeNull();
  });
});

describe("the crumb's Project name is fetched once, not on every revalidation", () => {
  it("asks the API when the name is unknown, remembers it, and never asks again", async () => {
    wire({ task: { id: T, project_id: S, title: "Memo" }, project: { id: S, name: "Acme" } });
    bar(`/projects/${S}/tasks/${T}`);
    await run(`/projects/${S}/tasks/${T}`, { projectId: S, id: T });
    expect(projectReads()).toBe(1);
    expect(knownProjectName(S)).toBe("Acme");

    const again = (await run(`/projects/${S}/tasks/${T}`, { projectId: S, id: T })) as {
      project: { name: string };
    };
    expect(projectReads()).toBe(1); // a revalidation reuses it
    expect(again.project.name).toBe("Acme");
  });

  it("uses a name the Project page or the sidebar already knows, with no request at all", async () => {
    rememberProjectName(S, "Launch");
    wire({ task: { id: T, project_id: S, title: "Memo" } });
    bar(`/projects/${S}/tasks/${T}`);
    const data = (await run(`/projects/${S}/tasks/${T}`, { projectId: S, id: T })) as {
      project: { id: string; name: string };
    };
    expect(projectReads()).toBe(0);
    expect(data.project).toEqual({ id: S, name: "Launch" });
  });

  it("never attributes a guessed Project's name to the task's own Project", async () => {
    // the task lives in S; the URL says OTHER: whatever OTHER is called, S is not told it
    wire({
      task: { id: T, project_id: S, title: "Memo" },
      project: { id: OTHER, name: "Elsewhere" },
    });
    bar("/inbox");
    await redirected(`/projects/${OTHER}/tasks/${T}`, { projectId: OTHER, id: T });
    expect(knownProjectName(S)).toBeUndefined();
  });
});

describe("the crumb", () => {
  const trail = handle.trail as (
    d: unknown,
    params?: Record<string, string | undefined>,
  ) => { label: string; to?: string }[] | null;

  it("a Project task crumbs Projects > the Project (a link back) > the task", () => {
    expect(
      trail({
        detail: { id: T, project_id: S, title: "Memo", goal: "g" },
        project: { id: S, name: "Acme" },
      }),
    ).toEqual([
      { label: "Projects", to: "/projects" },
      { label: "Acme", to: `/projects/${S}` },
      { label: "Memo" },
    ]);
  });

  it("a Project whose name could not be read still links back", () => {
    expect(
      trail({ detail: { id: T, project_id: S, title: null, goal: "Write it" }, project: null }),
    ).toEqual([
      { label: "Projects", to: "/projects" },
      { label: "Project", to: `/projects/${S}` },
      { label: "Write it" },
    ]);
  });

  it("a personal task keeps the path-derived crumb (Tasks > title)", () => {
    expect(
      trail({ detail: { id: T, project_id: null, title: "Memo", goal: "g" }, project: null }),
    ).toBeNull();
  });

  it("a nested URL whose task did not load (not found) still has a live trail, never a dead 'Tasks' link", () => {
    // loaderData is undefined in the error state; the URL's own params are all there is
    expect(trail(undefined, { projectId: S, id: T })).toEqual([
      { label: "Projects", to: "/projects" },
      { label: "Project", to: `/projects/${S}` },
      { label: "Task" },
    ]);
  });

  it("a personal task's error state keeps the path-derived crumb", () => {
    expect(trail(undefined, { id: T })).toBeNull();
  });
});
