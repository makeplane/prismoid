/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The forgot-password page: the email alone in the POST body, the sent card after it, Try again
// back to a form ready to send, and a rate limit worded with its wait.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider, useActionData } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";

import ForgotPassword, { clientAction } from "./route";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

type Props = Parameters<typeof ForgotPassword>[0];
function Page() {
  const props = { actionData: useActionData() };
  return <ForgotPassword {...(props as unknown as Props)} />;
}
function mount() {
  const router = createMemoryRouter(
    [
      {
        path: "/forgot-password",
        Component: Page,
        action: (args) => clientAction(args as never),
      },
    ],
    { initialEntries: ["/forgot-password"] },
  );
  render(<RouterProvider router={router} />);
}
const accepted = () =>
  vi.spyOn(api, "POST").mockImplementation((() =>
    Promise.resolve({
      data: { ok: true },
      error: undefined,
      response: new Response(null, { status: 202 }),
    })) as never);

describe("forgot-password", () => {
  it("sends the email alone and shows the sent card", async () => {
    const post = accepted();
    const user = userEvent.setup();
    mount();
    await user.type(await screen.findByLabelText(/^Email/), "ada@example.com");
    const submit = screen.getByRole("button", { name: "Send reset code" });
    expect(submit.hasAttribute("disabled")).toBe(false);
    await user.click(submit);
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(post.mock.calls[0]![1]).toEqual({ body: { email: "ada@example.com" } });
    expect(await screen.findByText("Check your email")).toBeTruthy();
  });

  it("Try again after a send is back to a form ready to send", async () => {
    accepted();
    const user = userEvent.setup();
    mount();
    await user.type(await screen.findByLabelText(/^Email/), "ada@example.com");
    await user.click(screen.getByRole("button", { name: "Send reset code" }));
    await user.click(await screen.findByRole("link", { name: "Try again" }));
    const again = await screen.findByRole("button", { name: "Send reset code" });
    expect(again.hasAttribute("disabled")).toBe(false);
  });

  it("a 429 says how long to wait", async () => {
    vi.spyOn(api, "POST").mockImplementation((() =>
      Promise.resolve({
        data: undefined,
        error: { error: { code: "rate_limited", message: "server words" } },
        response: new Response(null, { status: 429, headers: { "Retry-After": "12" } }),
      })) as never);
    const user = userEvent.setup();
    mount();
    await user.type(await screen.findByLabelText(/^Email/), "ada@example.com");
    await user.click(screen.getByRole("button", { name: "Send reset code" }));
    expect(await screen.findByText(/Too many attempts\. Try again in \d+s\./)).toBeTruthy();
  });
});
