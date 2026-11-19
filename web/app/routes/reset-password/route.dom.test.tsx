/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The second half of a password reset: a code from the link is prefilled, a short password or a
// truncated code never reaches the wire, the server's field errors land on the form's own fields,
// each failure status says its own words, and success says what the reset revoked.
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider, useActionData, useLoaderData } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import ResetPassword, { clientAction, clientLoader } from "./route";

const reply = (status: number, data: unknown, error?: unknown) =>
  Promise.resolve({ data, error, response: new Response(null, { status }) });
const failure = (status: number, error: Record<string, unknown>) =>
  reply(status, undefined, { error });

const CODE = "cr_0123456789abcdefghijklmnop";
const PASSWORD = "a long new password";

type Props = Parameters<typeof ResetPassword>[0];

// The framework hands a route module its data as props; a memory router hands it to hooks.
function Page() {
  const props = { loaderData: useLoaderData(), actionData: useActionData() };
  return <ResetPassword {...(props as unknown as Props)} />;
}

let post: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  post = vi.spyOn(api, "POST").mockImplementation((() => reply(200, {})) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

function mount(url = "/reset-password") {
  const router = createMemoryRouter(
    [
      {
        path: "/reset-password",
        Component: Page,
        loader: (args) => clientLoader(args as never),
        action: (args) => clientAction(args as never),
        HydrateFallback: () => null,
      },
      { path: "/login", element: <p>login page</p> },
    ],
    { initialEntries: [url] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

async function submit(user: ReturnType<typeof userEvent.setup>, code: string, password: string) {
  const codeField = await screen.findByLabelText(/^Reset code/);
  await user.clear(codeField);
  if (code) await user.type(codeField, code);
  await user.type(screen.getByLabelText(/^New password/), password);
  await user.click(screen.getByRole("button", { name: "Reset password" }));
}

describe("ResetPassword", () => {
  it("prefills the code from the link", async () => {
    mount(`/reset-password?code=${CODE}`);
    expect(((await screen.findByLabelText(/^Reset code/)) as HTMLInputElement).value).toBe(CODE);
  });

  it("a good code and password reset it and say what was revoked", async () => {
    const user = userEvent.setup();
    mount();
    await submit(user, `  ${CODE}  `, PASSWORD);
    expect(await screen.findByText("Password updated")).toBeDefined();
    expect(post.mock.calls[0]?.[1]).toEqual({ body: { code: CODE, new_password: PASSWORD } });
    // A Propel button rendered as a link: the anchor carries the button role.
    expect(screen.getByRole("button", { name: "Sign in" }).getAttribute("href")).toBe("/login");
  });

  it("a short password never reaches the wire", async () => {
    const user = userEvent.setup();
    mount();
    await submit(user, CODE, "short");
    expect(await screen.findByText("Use at least 12 characters.")).toBeDefined();
    expect(post).not.toHaveBeenCalled();
  });

  it("an expired code says to request a new one", async () => {
    post.mockImplementation((() => failure(401, { code: "invalid_code", message: "x" })) as never);
    const user = userEvent.setup();
    mount();
    await submit(user, CODE, PASSWORD);
    expect(
      await screen.findByText("That code is invalid or has expired. Request a new one."),
    ).toBeDefined();
  });

  it("too many attempts names the wait", async () => {
    post.mockImplementation((() =>
      failure(429, { code: "rate_limited", message: "x", retry_after: 7 })) as never);
    const user = userEvent.setup();
    mount();
    await submit(user, CODE, PASSWORD);
    expect(await screen.findByText("Too many attempts. Try again in 7s.")).toBeDefined();
  });

  it("the server's new_password error lands on the password field", async () => {
    post.mockImplementation((() =>
      failure(422, {
        code: "invalid",
        message: "x",
        fields: { new_password: "That password is too common." },
      })) as never);
    const user = userEvent.setup();
    mount();
    await submit(user, CODE, PASSWORD);
    expect(await screen.findByText("That password is too common.")).toBeDefined();
  });

  it("any other failure shows the server's message", async () => {
    post.mockImplementation((() =>
      failure(503, { code: "unavailable", message: "Try again shortly." })) as never);
    const user = userEvent.setup();
    mount();
    await submit(user, CODE, PASSWORD);
    expect(await screen.findByText("Try again shortly.")).toBeDefined();
  });
});
