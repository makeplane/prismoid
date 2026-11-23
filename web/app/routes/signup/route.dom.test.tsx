/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The signup page reads the deployment's public config: paused when signups are invite-only (an
// invite link still gets the full form, and an emailed code signs up through the paused page's own
// form), paused when the server says signup_disabled on submit, and a consent line that names only
// the legal links this deployment sets.
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider, useActionData, useLoaderData } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import { OPEN_CONFIG, type AuthConfig } from "~/lib/auth-config";
import { STORAGE } from "~/lib/storage";

const state = vi.hoisted(() => ({ config: null as unknown as AuthConfig }));
vi.mock("~/lib/auth-config", async (orig) => ({
  ...(await orig<typeof import("~/lib/auth-config")>()),
  getAuthConfig: async () => state.config,
}));

import Signup, { clientAction, clientLoader } from "./route";

const failure = (status: number, code: string, message: string) =>
  Promise.resolve({
    data: undefined,
    error: { error: { code, message } },
    response: new Response(null, { status }),
  });

beforeEach(() => {
  localStorage.clear();
  state.config = OPEN_CONFIG;
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
  document.body.innerHTML = "";
});

type Props = Parameters<typeof Signup>[0];
function Page() {
  const props = { loaderData: useLoaderData(), actionData: useActionData() };
  return <Signup {...(props as unknown as Props)} />;
}
function mount(url = "/signup") {
  const router = createMemoryRouter(
    [
      {
        path: "/signup",
        Component: Page,
        loader: (args) => clientLoader(args as never),
        action: (args) => clientAction(args as never),
        HydrateFallback: () => null,
      },
      { path: "/", Component: () => <p>home</p> },
    ],
    { initialEntries: [url] },
  );
  render(<RouterProvider router={router} />);
}

describe("signups paused", () => {
  it("shows Signups are paused when the config says invite and the URL has no invite", async () => {
    state.config = { ...OPEN_CONFIG, signup: "invite" };
    mount();
    expect(await screen.findByRole("heading", { name: "Signups are paused" })).toBeTruthy();
    expect(
      screen.getByText(
        "Prismoid isn't taking new accounts right now. If you have an invite, open the link in your email.",
      ),
    ).toBeTruthy();
    expect(
      screen.getByText("Got a code in your invite email? Paste it below to sign up."),
    ).toBeTruthy();
    expect(screen.getByLabelText(/^Invite code/).getAttribute("aria-required")).toBe("true");
    expect(screen.getByRole("link", { name: "Sign in" })).toBeTruthy();
  });

  it("an emailed code pasted on the paused page signs up, with no ?invite= in the URL", async () => {
    state.config = { ...OPEN_CONFIG, signup: "invite" };
    const post = vi.spyOn(api, "POST").mockImplementation((() =>
      Promise.resolve({
        data: { token: "cs_new", expires_at: "2026-12-01T00:00:00Z" },
        error: undefined,
        response: new Response(null, { status: 201 }),
      })) as never);
    const user = userEvent.setup();
    mount();
    await user.type(await screen.findByLabelText(/^Name/), "Ada");
    await user.type(screen.getByLabelText(/^Email/), "ada@example.com");
    await user.type(screen.getByLabelText(/^Password/), "a-long-enough-password");
    await user.type(screen.getByLabelText(/^Invite code/), "oi_from_the_email");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByText("home")).toBeTruthy();
    expect(post.mock.calls[0]![1]).toMatchObject({ body: { invite_code: "oi_from_the_email" } });
    expect(localStorage.getItem(STORAGE.token)).toBe("cs_new");
  });

  it("the paused page's form sends nothing without a code", async () => {
    state.config = { ...OPEN_CONFIG, signup: "invite" };
    const post = vi.spyOn(api, "POST");
    const user = userEvent.setup();
    mount();
    await user.type(await screen.findByLabelText(/^Name/), "Ada");
    await user.type(screen.getByLabelText(/^Email/), "ada@example.com");
    await user.type(screen.getByLabelText(/^Password/), "a-long-enough-password");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByText("Enter the code from your invite email.")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Signups are paused" })).toBeTruthy();
    expect(post).not.toHaveBeenCalled();
  });

  it("keeps the form for an invite link while signups are paused", async () => {
    state.config = { ...OPEN_CONFIG, signup: "invite" };
    mount("/signup?invite=ci_abc");
    expect(await screen.findByLabelText(/^Email/)).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Signups are paused" })).toBeNull();
  });

  it("a 403 signup_disabled on submit switches to the paused page", async () => {
    vi.spyOn(api, "POST").mockImplementation((() =>
      failure(403, "signup_disabled", "Signups are paused.")) as never);
    const user = userEvent.setup();
    mount();
    await user.type(await screen.findByLabelText(/^Name/), "Ada");
    await user.type(screen.getByLabelText(/^Email/), "ada@example.com");
    await user.type(screen.getByLabelText(/^Password/), "a-long-enough-password");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByRole("heading", { name: "Signups are paused" })).toBeTruthy();
  });

  it("a refused address gets the form's own sentence, never the server's vague one", async () => {
    vi.spyOn(api, "POST").mockImplementation((() =>
      failure(403, "forbidden", "that email cannot be registered")) as never);
    const user = userEvent.setup();
    mount();
    await user.type(await screen.findByLabelText(/^Name/), "Ada");
    await user.type(screen.getByLabelText(/^Email/), "ada@example.com");
    await user.type(screen.getByLabelText(/^Password/), "a-long-enough-password");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByText(EMAIL_REFUSED)).toBeTruthy();
    expect(screen.queryByText("that email cannot be registered")).toBeNull();
  });
});

const EMAIL_REFUSED =
  "That email can't be used to sign up. If it already has an account, sign in instead.";

describe("the consent line", () => {
  it("links Terms and Privacy Policy to the configured URLs", async () => {
    state.config = {
      ...OPEN_CONFIG,
      terms_url: "https://example.test/terms",
      privacy_url: "https://example.test/privacy",
    };
    mount();
    expect(await screen.findByText(/By signing up you agree to the/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Terms" }).getAttribute("href")).toBe(
      "https://example.test/terms",
    );
    expect(screen.getByRole("link", { name: "Privacy Policy" }).getAttribute("href")).toBe(
      "https://example.test/privacy",
    );
  });

  it("is absent when neither is set", async () => {
    mount();
    await screen.findByLabelText(/^Email/);
    expect(screen.queryByText(/By signing up you agree/)).toBeNull();
  });
});
