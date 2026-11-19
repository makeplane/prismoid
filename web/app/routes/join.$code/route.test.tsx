/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// /join/:code - a Project join link (ADR-062). Logged out it goes to login and back to the same
// link; logged in it accepts inside the loader and opens the Project (the server already moved the
// session into the Project's workspace); a dead link is a face with the server's own words, not a crash.
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router";
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

import Join, { clientLoader, meta } from "./route";

// The framework hands the page its Route.ComponentProps; only `loaderData` is read.
const Page = Join as unknown as (p: { loaderData: { error: string } }) => React.JSX.Element;

const call = (code = "si_abc") =>
  clientLoader({
    params: { code },
    request: new Request(`http://x/join/${code}`),
  } as never);

beforeEach(() => state.post.mockReset());

describe("/join/:code", () => {
  it("logged out: goes to login carrying the link to come back to", async () => {
    state.token = null;
    const res = (await call("si_abc").catch((e: unknown) => e)) as Response;
    expect(res.status).toBe(302);
    expect(res.headers.get("Location")).toBe("/login?next=%2Fjoin%2Fsi_abc");
    expect(state.post).not.toHaveBeenCalled();
  });

  it("logged in: accepts the code, then opens the Project it named", async () => {
    state.token = "t";
    state.post.mockResolvedValue({ project_id: "sp1", workspace_id: "o1", role: "editor" });
    const res = (await call("si_abc").catch((e: unknown) => e)) as Response;
    expect(state.post).toHaveBeenCalledWith("/v1/projects/invites/accept", {
      body: { code: "si_abc" },
    });
    expect(res.status).toBe(302);
    expect(res.headers.get("Location")).toBe("/projects/sp1");
  });

  it("a dead link is data for the page, never a throw", async () => {
    state.token = "t";
    state.post.mockResolvedValue({
      err: new state.ApiError(404, "not_found", "this invite is invalid or has expired"),
    });
    const out = (await call("si_abc")) as { error: string };
    expect(out.error).toBe("this invite is invalid or has expired");
  });

  it("an error that is not the API's own still reaches the error boundary", async () => {
    state.token = "t";
    state.post.mockResolvedValue({ err: new TypeError("offline") });
    await expect(call("si_abc")).rejects.toThrow("offline");
  });

  it("the failure face says Couldn't join, in the server's words, with a way home", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <Page loaderData={{ error: "this invite is invalid or has expired" }} />
      </MemoryRouter>,
    );
    expect(html).toContain("Couldn&#x27;t join");
    expect(html).toContain("this invite is invalid or has expired");
    expect(html).toContain('href="/"');
  });

  it("titles the tab", () => {
    expect(meta()).toEqual([{ title: "Join project · Prismoid" }]);
  });
});
