/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** Signup takes an invite code in plain sight: the emailed code is
 *  pasted here, or pre-filled by /invite/:code, and rides the signup body as invite_code. */
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ post: vi.fn(), stored: vi.fn() }));
vi.mock("~/lib/api/client", async () => ({
  getToken: () => null,
  storeSession: state.stored,
  api: { POST: state.post },
  unwrap: async (p: Promise<unknown>) => p,
  ApiError: class extends Error {},
}));

import { clientAction, InviteCodeField } from "./route";

const submit = (fields: Record<string, string>) => {
  const body = new FormData();
  for (const [k, v] of Object.entries(fields)) body.set(k, v);
  return clientAction({
    request: new Request("http://x/signup", { method: "POST", body }),
  } as never).catch((e: unknown) => e);
};
const person = { name: "Bob", email: "bob@example.com", password: "a-long-enough-password" };

beforeEach(() => {
  state.post.mockReset();
  state.post.mockResolvedValue({ token: "cs_t", expires_at: "2099-01-01T00:00:00Z" });
});

describe("the invite code field", () => {
  it("is a visible, optional input carrying the pre-filled code", () => {
    const html = renderToStaticMarkup(<InviteCodeField defaultValue="ci_abc" />);
    expect(html).toContain('name="invite"');
    expect(html).not.toContain('type="hidden"');
    expect(html).toContain('value="ci_abc"');
    expect(html).toContain("Invite code (optional)");
  });

  it("a pasted code rides the signup as invite_code; an empty one is left out", async () => {
    await submit({ ...person, invite: "  ci_pasted " });
    expect(state.post).toHaveBeenLastCalledWith("/v1/auth/signup", {
      body: { ...person, invite_code: "ci_pasted" },
    });
    await submit({ ...person, invite: "" });
    expect(state.post).toHaveBeenLastCalledWith("/v1/auth/signup", { body: person });
  });
});
