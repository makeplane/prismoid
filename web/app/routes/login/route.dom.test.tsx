/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The product's front door: a good sign-in stores the session and lands on the page asked for,
// each failure status says its own words and keeps the form, a signed-in visitor skips the form,
// and a hostile `next` never reaches the page or the redirect.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider, useActionData, useLoaderData } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, getToken, storeSession } from "~/lib/api/client";
import Login, { clientAction, clientLoader } from "./route";

const reply = (status: number, data: unknown, error?: unknown) =>
  Promise.resolve({ data, error, response: new Response(null, { status }) });
const failure = (status: number, code: string, message: string, retry?: number) =>
  reply(status, undefined, { error: { code, message, retry_after: retry } });

let post: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  localStorage.clear();
  post = vi
    .spyOn(api, "POST")
    .mockImplementation((() =>
      reply(200, { token: "cs_tok", expires_at: "2027-01-01T00:00:00Z" })) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
  document.body.innerHTML = "";
});

type Props = Parameters<typeof Login>[0];

// The framework hands a route module its data as props; a memory router hands it to hooks.
function Page() {
  const props = { loaderData: useLoaderData(), actionData: useActionData() };
  return <Login {...(props as unknown as Props)} />;
}

function mount(url = "/login") {
  const router = createMemoryRouter(
    [
      {
        path: "/login",
        Component: Page,
        loader: (args) => clientLoader(args as never),
        action: (args) => clientAction(args as never),
        HydrateFallback: () => null,
      },
      { path: "/", element: <p>home page</p> },
      { path: "/projects", element: <p>projects page</p> },
    ],
    { initialEntries: [url] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

async function signIn(user: ReturnType<typeof userEvent.setup>, email = "ada@example.com") {
  await user.type(await screen.findByLabelText(/^Email/), email);
  await user.type(screen.getByLabelText(/^Password/), "correct horse");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("Login", () => {
  it("signs in, stores the session and lands on the page asked for", async () => {
    const user = userEvent.setup();
    const router = mount("/login?next=/projects");
    await signIn(user);
    await waitFor(() => expect(router.state.location.pathname).toBe("/projects"));
    expect(getToken()).toBe("cs_tok");
    expect(post.mock.calls[0]?.[1]).toEqual({
      body: { email: "ada@example.com", password: "correct horse" },
    });
  });

  it("with no next, a sign-in lands home", async () => {
    const user = userEvent.setup();
    const router = mount();
    await signIn(user);
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
  });

  it("a wrong password says so and keeps the person on the form", async () => {
    post.mockImplementation((() => failure(401, "invalid_credentials", "nope")) as never);
    const user = userEvent.setup();
    const router = mount();
    await signIn(user);
    expect(await screen.findByText("Invalid email or password.")).toBeDefined();
    expect(router.state.location.pathname).toBe("/login");
    expect(getToken()).toBeNull();
  });

  it("too many attempts names the wait", async () => {
    post.mockImplementation((() => failure(429, "rate_limited", "slow down", 42)) as never);
    const user = userEvent.setup();
    mount();
    await signIn(user);
    expect(await screen.findByText("Too many attempts. Try again in 42s.")).toBeDefined();
  });

  it("any other failure shows the server's message", async () => {
    post.mockImplementation((() =>
      failure(403, "suspended", "This account is suspended.")) as never);
    const user = userEvent.setup();
    mount();
    await signIn(user);
    expect(await screen.findByText("This account is suspended.")).toBeDefined();
  });

  it("a field error from the server lands on its field", async () => {
    post.mockImplementation((() =>
      reply(422, undefined, {
        error: { code: "invalid", message: "bad", fields: { email: "No such address." } },
      })) as never);
    const user = userEvent.setup();
    mount();
    await signIn(user);
    expect(await screen.findByText("No such address.")).toBeDefined();
  });

  it("a signed-in visitor skips the form", async () => {
    storeSession("cs_already", "2027-01-01T00:00:00Z");
    const router = mount("/login?next=/projects");
    await waitFor(() => expect(router.state.location.pathname).toBe("/projects"));
    expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull();
  });

  it("a hostile next is never echoed into the signup link", async () => {
    mount("/login?next=https://evil.example/steal");
    const link = await screen.findByRole("link", { name: "Create an account" });
    expect(link.getAttribute("href")).not.toContain("evil");
    expect(screen.getByRole("link", { name: "Forgot your password?" }).getAttribute("href")).toBe(
      "/forgot-password",
    );
  });
});
