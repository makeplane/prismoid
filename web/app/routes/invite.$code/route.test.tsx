/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => {
  class ApiError extends Error {
    constructor(
      public status: number,
      public code: string,
      message: string,
    ) {
      super(message);
    }
  }
  return { token: null as string | null, post: vi.fn(), ApiError };
});
vi.mock("~/lib/api/client", async () => ({
  getToken: () => state.token,
  api: { POST: state.post },
  // The wire's failure is DATA here ({ err }) and unwrap raises it - a mock that returns a
  // rejected promise trips vitest's own rejection tracking even when the code handles it.
  unwrap: async (p: Promise<{ err?: Error }>) => {
    const v = await p;
    if (v?.err) throw v.err;
    return v;
  },
  ApiError: state.ApiError,
}));

import { clientLoader } from "./route";

const call = (code = "abc") =>
  clientLoader({
    params: { code },
    request: new Request(`http://x/invite/${code}`),
  } as never);

beforeEach(() => state.post.mockReset());

describe("/invite/:code", () => {
  it("unauthenticated: routes through signup carrying the code", async () => {
    state.token = null;
    const res = (await call("a b").catch((e: unknown) => e)) as Response;
    expect(res.status).toBe(302);
    expect(res.headers.get("Location")).toBe("/signup?invite=a%20b");
    expect(state.post).not.toHaveBeenCalled();
  });

  it("authenticated: posts accept, then goes home", async () => {
    state.token = "t";
    state.post.mockResolvedValue({ id: "o1" });
    const res = (await call("abc").catch((e: unknown) => e)) as Response;
    expect(state.post).toHaveBeenCalledWith("/v1/workspaces/invites/accept", {
      body: { code: "abc" },
    });
    expect(res.headers.get("Location")).toBe("/");
  });

  it("a refused accept (wrong email) is a face, not a crash", async () => {
    state.token = "t";
    state.post.mockResolvedValue({
      err: new state.ApiError(403, "forbidden", "This invite is for another email."),
    });
    const out = (await call("abc")) as { error: string };
    expect(out.error).toContain("another email");
  });
});
