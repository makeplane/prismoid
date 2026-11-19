/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The landing no longer takes a Project preselect: a Project starts its own
// tasks from its own page. Its picker and its last-used memory remain - those are the landing's.
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
vi.mock("~/components/composer/task-composer", async (orig) => ({
  ...(await orig<typeof import("~/components/composer/task-composer")>()),
  loadComposerOptions: async () => ({
    skills: [],
    fleetModels: [],
    connectors: [],
    catalog: null,
    bots: [],
  }),
}));

import { clientLoader } from "./route";

const run = (url: string) =>
  (clientLoader as unknown as (a: unknown) => Promise<{ initialProject: unknown }>)({
    request: new Request(`http://app.test${url}`),
  });

const projectReads = () => state.get.mock.calls.filter(([p]) => p === "/v1/projects/{project_id}");

function remember(value: unknown) {
  const store = new Map<string, string>();
  if (value !== undefined) store.set("prismoid.lastspace", JSON.stringify(value));
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  });
  return store;
}

beforeEach(() => {
  state.get.mockReset();
  state.get.mockImplementation(
    async (_path: string, opts: { params: { path: { project_id: string } } }) => ({
      id: opts.params.path.project_id,
      name: `Project ${opts.params.path.project_id}`,
    }),
  );
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the landing's loader", () => {
  it("ignores ?project= entirely: no Project is preselected, none is even asked about", async () => {
    remember(undefined);
    const data = await run("/?project=5a1d8c52-0f4e-4a54-9d0b-0a3b1d8d2f10");
    expect(data.initialProject).toBeNull();
    expect(projectReads()).toHaveLength(0);
  });

  it("still files into the last Project used (destination memory), validated against the API", async () => {
    remember({ id: "s-last", name: "Last" });
    const data = await run("/");
    expect(data.initialProject).toEqual({ id: "s-last", name: "Project s-last" });
    expect(projectReads()).toHaveLength(1);
  });

  it("memory wins over a ?project= in the URL, never the other way round", async () => {
    remember({ id: "s-last", name: "Last" });
    const data = await run("/?project=s-other");
    expect(data.initialProject).toEqual({ id: "s-last", name: "Project s-last" });
    expect(projectReads().map(([, o]) => o.params.path.project_id)).toEqual(["s-last"]);
  });

  it("forgets a remembered Project that is gone", async () => {
    const store = remember({ id: "s-gone", name: "Gone" });
    state.get.mockImplementation(async () => ({ err: new Error("404") }));
    const data = await run("/");
    expect(data.initialProject).toBeNull();
    expect(store.has("prismoid.lastspace")).toBe(false);
  });
});
