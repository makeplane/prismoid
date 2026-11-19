/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The General settings tab: the profile loads and splits the name, Save is live only for a real
// change and PATCHes the joined name, a failed load or save says so, and the email is read-only.
import { toast } from "@prismoid/blocks/toast";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import { GeneralPanel } from "./general-panel";

const reply = (status: number, data: unknown, error?: unknown) =>
  Promise.resolve({ data, error, response: new Response(null, { status }) });

const me = {
  email: "ada@example.com",
  name: "Ada King Lovelace",
  timezone: "UTC",
  is_admin: false,
};

let get: ReturnType<typeof vi.spyOn>;
let patch: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  get = vi.spyOn(api, "GET").mockImplementation((() => reply(200, me)) as never);
  patch = vi
    .spyOn(api, "PATCH")
    .mockImplementation((() => reply(200, { ...me, name: "Ada Byron" })) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

function mount() {
  const router = createMemoryRouter([{ path: "/", Component: GeneralPanel }], {
    initialEntries: ["/"],
  });
  render(<RouterProvider router={router} />);
}

const field = (name: string) => screen.findByRole("textbox", { name }) as Promise<HTMLInputElement>;

describe("GeneralPanel", () => {
  it("splits the stored name into first and rest, with a read-only email", async () => {
    mount();
    expect((await field("First name")).value).toBe("Ada");
    expect((await field("Last name")).value).toBe("King Lovelace");
    const email = (await field("Email")) as HTMLInputElement;
    expect(email.value).toBe("ada@example.com");
    expect(email.disabled).toBe(true);
  });

  it("Save is off until the name changes, then PATCHes the joined name and confirms", async () => {
    const user = userEvent.setup();
    const success = vi.spyOn(toast, "success");
    mount();
    const save = await screen.findByRole("button", { name: "Save" });
    expect(save.hasAttribute("disabled")).toBe(true);
    const last = await field("Last name");
    await user.clear(last);
    await user.type(last, "Byron");
    await user.click(save);
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(patch.mock.calls[0]?.[0]).toBe("/v1/auth/me");
    expect(patch.mock.calls[0]?.[1]).toEqual({ body: { name: "Ada Byron" } });
    expect(success).toHaveBeenCalledWith("Profile saved.");
  });

  it("clearing both names leaves Save off", async () => {
    const user = userEvent.setup();
    mount();
    await user.clear(await field("First name"));
    await user.clear(await field("Last name"));
    expect(screen.getByRole("button", { name: "Save" }).hasAttribute("disabled")).toBe(true);
  });

  it("a failed save toasts the server's words", async () => {
    const user = userEvent.setup();
    const error = vi.spyOn(toast, "error");
    patch.mockImplementation((() =>
      reply(422, undefined, { error: { code: "x", message: "Name is too long." } })) as never);
    mount();
    const first = await field("First name");
    await user.type(first, "x");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("Name is too long."));
  });

  it("a failed profile load toasts and leaves the loader", async () => {
    const error = vi.spyOn(toast, "error");
    get.mockImplementation((() =>
      reply(500, undefined, { error: { code: "x", message: "no" } })) as never);
    mount();
    await waitFor(() => expect(error).toHaveBeenCalledWith("Couldn't load your profile."));
    expect(screen.queryByRole("textbox", { name: "First name" })).toBeNull();
  });

  it("the theme chips are all offered", async () => {
    mount();
    await field("First name");
    for (const label of ["Light", "Dark", "System"])
      expect(screen.getByRole("button", { name: label })).toBeDefined();
  });
});
