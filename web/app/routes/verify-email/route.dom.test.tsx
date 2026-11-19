/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The emailed link carries the code in its fragment: the page reads it, keeps it across a sign-in
// detour in sessionStorage, posts it once, and says what happened.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider, useLoaderData, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, setUnauthorizedHandler } from "~/lib/api/client";
import { STORAGE } from "~/lib/storage";

import VerifyEmail, { clientLoader } from "./route";

const CODE = "ev_" + "a".repeat(43);

const reply = (status: number, body: unknown) =>
  Promise.resolve({
    data: status < 400 ? body : undefined,
    error: status < 400 ? undefined : { error: { code: "unauthorized", message: "nope" } },
    response: new Response(null, { status }),
  });

const bodyOf = (call: unknown[] | undefined) => (call?.[1] as { body: unknown } | undefined)?.body;

function Probe() {
  return <span data-testid="at">{useLocation().pathname + useLocation().search}</span>;
}
type Props = Parameters<typeof VerifyEmail>[0];
function Page() {
  return <VerifyEmail {...({ loaderData: useLoaderData() } as unknown as Props)} />;
}
function mount() {
  const router = createMemoryRouter(
    [
      {
        path: "/verify-email",
        Component: Page,
        loader: () => clientLoader(),
        HydrateFallback: () => null,
      },
      { path: "/login", Component: Probe },
    ],
    { initialEntries: ["/verify-email"] },
  );
  render(<RouterProvider router={router} />);
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  window.location.hash = `#${CODE}`;
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
  sessionStorage.clear();
  window.location.hash = "";
});

describe("verify-email", () => {
  it("posts the fragment's code once and says the address is verified", async () => {
    localStorage.setItem(STORAGE.token, "cs_x");
    const post = vi.spyOn(api, "POST").mockImplementation((() => reply(200, {})) as never);
    mount();
    expect(await screen.findByText("Your email address is verified.")).toBeTruthy();
    expect(post).toHaveBeenCalledTimes(1);
    expect(post.mock.calls[0]?.[0]).toBe("/v1/auth/me/verify-email");
    expect(bodyOf(post.mock.calls[0])).toEqual({ code: CODE });
    expect(window.location.hash).toBe(""); // the fragment is not left in the address bar
    expect(sessionStorage.getItem(STORAGE.verifyCode)).toBeNull(); // success forgets the code
  });

  it("signed out, keeps the code and goes to sign-in, then reads it back", async () => {
    const post = vi.spyOn(api, "POST").mockImplementation((() => reply(200, {})) as never);
    mount();
    await waitFor(() =>
      expect(screen.getByTestId("at").textContent).toBe("/login?next=%2Fverify-email"),
    );
    expect(sessionStorage.getItem(STORAGE.verifyCode)).toBe(CODE);
    expect(post).not.toHaveBeenCalled();
    cleanup();

    localStorage.setItem(STORAGE.token, "cs_x"); // back from sign-in: no fragment, the stored code
    window.location.hash = "";
    mount();
    expect(await screen.findByText("Your email address is verified.")).toBeTruthy();
    expect(bodyOf(post.mock.calls[0])).toEqual({ code: CODE });
    expect(sessionStorage.getItem(STORAGE.verifyCode)).toBeNull();
  });

  it("on a 422 says the link expired, forgets the code, and resends from the page", async () => {
    localStorage.setItem(STORAGE.token, "cs_x");
    const post = vi
      .spyOn(api, "POST")
      .mockImplementation(((path: string) =>
        path === "/v1/auth/me/verify-email"
          ? reply(422, null)
          : reply(202, { ok: true })) as never);
    mount();
    expect(
      await screen.findByText("That link has expired. Send a new one from the banner."),
    ).toBeTruthy();
    expect(sessionStorage.getItem(STORAGE.verifyCode)).toBeNull();
    expect(sessionStorage.getItem(STORAGE.verifyCode)).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Resend" }));
    expect(await screen.findByText("Sent. Check your email.")).toBeTruthy();
    expect(post.mock.calls.at(-1)?.[0]).toBe("/v1/auth/me/verification");
  });

  it("a failed Resend stays on the expired page and says the resend failed", async () => {
    localStorage.setItem(STORAGE.token, "cs_x");
    vi.spyOn(api, "POST").mockImplementation(((path: string) =>
      path === "/v1/auth/me/verify-email" ? reply(422, null) : reply(429, null)) as never);
    mount();
    await screen.findByText("That link has expired. Send a new one from the banner.");
    await userEvent.click(screen.getByRole("button", { name: "Resend" }));
    expect(
      await screen.findByText("We couldn't send a new link. Try again in a few minutes."),
    ).toBeTruthy();
    expect(screen.getByText("That link has expired. Send a new one from the banner.")).toBeTruthy();
    expect(screen.queryByText(/Your link is kept/)).toBeNull();
  });

  it("on a 401 (a dead session) keeps the code and signs out instead of saying expired", async () => {
    localStorage.setItem(STORAGE.token, "cs_old");
    const signOut = vi.fn();
    setUnauthorizedHandler(signOut);
    vi.spyOn(api, "POST").mockImplementation((() => reply(401, null)) as never);
    mount();
    await waitFor(() => expect(signOut).toHaveBeenCalledTimes(1));
    expect(sessionStorage.getItem(STORAGE.verifyCode)).toBe(CODE);
    expect(screen.queryByText(/expired/)).toBeNull();
  });

  it("on a 5xx keeps the code and offers a retry", async () => {
    localStorage.setItem(STORAGE.token, "cs_x");
    vi.spyOn(api, "POST").mockImplementation((() => reply(503, null)) as never);
    mount();
    expect(await screen.findByText(/Your link is kept/)).toBeTruthy();
    expect(sessionStorage.getItem(STORAGE.verifyCode)).toBe(CODE);
  });
});
