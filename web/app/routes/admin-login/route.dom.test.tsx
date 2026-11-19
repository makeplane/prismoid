/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The operator console's front door: a malformed email never reaches the wire, a non-admin
// account is signed straight back out, each failure status says its own words, and an admin
// lands on the page they asked for. A signed-in visitor is redirected away at the loader.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, getToken, storeSession } from "~/lib/api/client";
import AdminLogin, { clientAction, clientLoader } from "./route";

const reply = (status: number, data: unknown, error?: unknown) =>
  Promise.resolve({ data, error, response: new Response(null, { status }) });
const failure = (status: number, code: string, message: string, retry?: number) =>
  reply(status, undefined, { error: { code, message, retry_after: retry } });

let post: ReturnType<typeof vi.spyOn>;
let get: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  localStorage.clear();
  post = vi
    .spyOn(api, "POST")
    .mockImplementation((() =>
      reply(200, { token: "tok", expires_at: "2027-01-01T00:00:00Z" })) as never);
  get = vi.spyOn(api, "GET").mockImplementation((() => reply(200, { is_admin: true })) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
  document.body.innerHTML = "";
});

function mount(url = "/login") {
  const router = createMemoryRouter(
    [
      {
        path: "/login",
        Component: AdminLogin,
        loader: (args) => clientLoader(args as never),
        action: (args) => clientAction(args as never),
        HydrateFallback: () => null,
      },
      { path: "/", element: <p>console home</p> },
      { path: "/users", element: <p>users page</p> },
    ],
    { initialEntries: [url] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

async function signIn(user: ReturnType<typeof userEvent.setup>, email: string, pw = "hunter2") {
  await user.type(await screen.findByLabelText(/^Email/), email);
  await user.type(screen.getByLabelText(/^Password/), pw);
  await user.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("AdminLogin", () => {
  it("a malformed email never reaches the wire", async () => {
    const user = userEvent.setup();
    mount();
    await signIn(user, "not-an-email");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDefined();
    expect(post).not.toHaveBeenCalled();
  });

  it("an admin signs in, the session is stored and they land on the page they asked for", async () => {
    const user = userEvent.setup();
    const router = mount("/login?next=/users");
    await signIn(user, "ops@example.com");
    await waitFor(() => expect(router.state.location.pathname).toBe("/users"));
    expect(getToken()).toBe("tok");
    expect(post.mock.calls[0]?.[1]).toEqual({
      body: { email: "ops@example.com", password: "hunter2" },
    });
  });

  it("a non-admin account is logged out again and told the console is for operators", async () => {
    const user = userEvent.setup();
    get.mockImplementation((() => reply(200, { is_admin: false })) as never);
    mount();
    await signIn(user, "pat@example.com");
    expect(await screen.findByText(/This console is for operators/)).toBeDefined();
    expect(post.mock.calls.map((c: unknown[]) => c[0])).toContain("/v1/auth/logout");
    expect(getToken()).toBeNull();
  });

  it.each([
    [401, "Invalid email or password."],
    [403, "This account is suspended."],
    [429, "Too many attempts. Try again in 12s."],
    [500, "Server fell over."],
  ])("a %i from the login says its own words", async (status, words) => {
    const user = userEvent.setup();
    post.mockImplementation((() => failure(status, "x", "Server fell over.", 12)) as never);
    mount();
    await signIn(user, "ops@example.com");
    expect(await screen.findByText(words)).toBeDefined();
  });

  it("a signed-in visitor is sent on by the loader instead of seeing the form", async () => {
    storeSession("tok", "2027-01-01T00:00:00Z");
    const router = mount("/login?next=/users");
    await waitFor(() => expect(router.state.location.pathname).toBe("/users"));
    expect(screen.queryByLabelText(/^Email/)).toBeNull();
  });
});
