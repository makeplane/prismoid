/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// A bot's Configure tab, driven: the kind facts and eval report render, Test evals / Draft for
// me / Publish call the right bot endpoints, the title and work dials save only when changed
// and with the right PATCH body, and a bad cap is refused before any request.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import { ConfigureTab } from "./configure-tab";

type Props = Parameters<typeof ConfigureTab>[0];

const bot = (over: Record<string, unknown> = {}) => ({
  name: "scout",
  version: 1,
  status: "published",
  eval_status: "green",
  eval_report: {},
  description: "Scouts leads",
  job: "",
  use_when: [],
  not_for: [],
  instructions: "Be brief",
  title: null,
  capability: "research",
  produces: "document",
  tools: ["web.fetch"],
  skills: ["cite"],
  connectors: ["gmail"],
  can_ask_user: true,
  cases: [],
  golden: {},
  work_mode: "auto",
  task_budget_usd: "5.00",
  threads: "one",
  project_id: null,
  created_at: "2026-11-01T10:00:00Z",
  ...over,
});

const ok = (data: unknown) => ({
  data,
  error: undefined,
  response: new Response(null, { status: 200 }),
});

let post: ReturnType<typeof vi.spyOn>;
let patch: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  vi.spyOn(api, "GET").mockImplementation((() => Promise.resolve(ok([]))) as never);
  post = vi.spyOn(api, "POST").mockImplementation((() => Promise.resolve(ok({}))) as never);
  patch = vi.spyOn(api, "PATCH").mockImplementation((() => Promise.resolve(ok({}))) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

function mount(head: unknown, draftAhead: unknown = null) {
  const onChanged = vi.fn();
  const props = { head, newest: draftAhead ?? head, draftAhead, onChanged } as unknown as Props;
  const router = createMemoryRouter([{ path: "/", element: <ConfigureTab {...props} /> }]);
  render(<RouterProvider router={router} />);
  return onChanged;
}

const patchBody = () =>
  (patch.mock.calls[patch.mock.calls.length - 1][1] as { body: Record<string, unknown> }).body;

describe("Kind facts and evals", () => {
  it("shows what the bot is and what it can reach", () => {
    mount(bot());
    for (const t of [
      "research",
      "publishes document",
      "web.fetch",
      "skill · cite",
      "connector · gmail",
    ])
      expect(screen.getByText(t)).toBeTruthy();
  });

  it("reports each eval case with the choice the selector made", () => {
    mount(
      bot({
        eval_status: "red",
        eval_report: {
          passed: 1,
          total: 2,
          cases: [
            { goal: "Find leads", expected: "scout", chosen: "scout", ok: true },
            { goal: "Write code", expected: "scout", chosen: "", ok: false },
          ],
        },
      }),
    );
    expect(screen.getByText("1/2")).toBeTruthy();
    expect(screen.getByText("→ scout")).toBeTruthy();
    expect(screen.getByText("→ none (want scout)")).toBeTruthy();
  });

  it("offers the share section only for a published head", async () => {
    mount(bot());
    expect(await screen.findByText("Share as template")).toBeTruthy();
    cleanup();
    mount(bot({ status: "draft" }));
    expect(screen.queryByText("Share as template")).toBeNull();
  });
});

describe("Header actions", () => {
  it("hides Test evals on a green bot and Draft for me on a published one", () => {
    mount(bot());
    expect(screen.queryByRole("button", { name: "Test evals" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Draft for me" })).toBeNull();
  });

  it("Test evals starts the evals for this bot", async () => {
    const user = userEvent.setup();
    mount(bot({ eval_status: "untested" }));
    await user.click(screen.getByRole("button", { name: "Test evals" }));
    expect(post).toHaveBeenCalledWith("/v1/bots/{name}/test", {
      params: { path: { name: "scout" }, query: { project_id: undefined } },
    });
  });

  it("Draft for me starts the drafter on a draft", async () => {
    const user = userEvent.setup();
    mount(bot({ status: "draft", eval_status: "green" }));
    await user.click(screen.getByRole("button", { name: "Draft for me" }));
    expect(post).toHaveBeenCalledWith("/v1/bots/{name}/draft", {
      params: { path: { name: "scout" }, query: { project_id: undefined } },
    });
  });

  it("offers Publish only for a green draft ahead, and publishes the head's name", async () => {
    const user = userEvent.setup();
    const onChanged = mount(bot(), bot({ version: 2, status: "draft" }));
    await user.click(screen.getByRole("button", { name: "Publish v2" }));
    expect(post).toHaveBeenCalledWith("/v1/bots/{name}/publish", {
      params: { path: { name: "scout" } },
    });
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("does not offer Publish while the draft's evals are not green", () => {
    mount(bot(), bot({ version: 2, status: "draft", eval_status: "red" }));
    expect(screen.queryByRole("button", { name: "Publish v2" })).toBeNull();
  });

  it("offers Publish for a green draft that was never published, and publishes it", async () => {
    const user = userEvent.setup();
    const onChanged = mount(bot({ status: "draft", version: 4 }));
    await user.click(screen.getByRole("button", { name: "Publish v4" }));
    expect(post).toHaveBeenCalledWith("/v1/bots/{name}/publish", {
      params: { path: { name: "scout" } },
    });
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("does not offer Publish for a never-published draft until its evals are green", () => {
    for (const eval_status of ["red", "untested"]) {
      mount(bot({ status: "draft", eval_status }));
      expect(screen.queryByRole("button", { name: /^Publish/ })).toBeNull();
      expect(screen.getByRole("button", { name: "Test evals" })).toBeTruthy();
      cleanup();
    }
  });

  it("does not offer Publish with no draft to publish", () => {
    for (const status of ["published", "archived"]) {
      mount(bot({ status }));
      expect(screen.queryByRole("button", { name: /^Publish/ })).toBeNull();
      cleanup();
    }
  });

  it("does not call the page changed when publish is refused", async () => {
    const user = userEvent.setup();
    post.mockResolvedValueOnce({
      data: undefined,
      error: { error: { code: "evals_red", message: "Evals are not green" } },
      response: new Response(null, { status: 409 }),
    } as never);
    const onChanged = mount(bot(), bot({ version: 2, status: "draft" }));
    await user.click(screen.getByRole("button", { name: "Publish v2" }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(onChanged).not.toHaveBeenCalled();
  });
});

describe("Title", () => {
  it("saves a typed title only once it differs, and clears it when emptied", async () => {
    const user = userEvent.setup();
    const onChanged = mount(bot({ title: "Old name" }));
    const input = screen.getByRole("textbox", { name: "Bot title" });
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
    await user.clear(input);
    await user.type(input, "Contracts desk");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(patchBody()).toMatchObject({ title: "Contracts desk", clear_title: false });
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    await user.clear(input);
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(patchBody()).toEqual({ clear_title: true, clear_icon: false, clear_hue: false });
  });
});

describe("Work dials", () => {
  const radio = (name: string) => screen.getByRole("radio", { name: new RegExp(`^${name}`) });

  it("starts at the saved mode with no Save button", () => {
    mount(bot({ work_mode: "ask" }));
    expect(radio("Ask").getAttribute("aria-checked")).toBe("true");
    expect(radio("Auto").getAttribute("aria-checked")).toBe("false");
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("saves the mode, threads and cap together", async () => {
    const user = userEvent.setup();
    mount(bot());
    await user.click(radio("Never"));
    await user.click(radio("Many chats"));
    const cap = screen.getByRole("textbox", { name: "Cap per task in dollars" });
    await user.clear(cap);
    await user.type(cap, "7.5");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(patchBody()).toMatchObject({
      work_mode: "never",
      threads: "many",
      task_budget_usd: 7.5,
    });
    await waitFor(() => expect((cap as HTMLInputElement).value).toBe("7.50"));
  });

  it("treats 5 and 5.00 as the same cap", async () => {
    const user = userEvent.setup();
    mount(bot());
    const cap = screen.getByRole("textbox", { name: "Cap per task in dollars" });
    await user.clear(cap);
    await user.type(cap, "5");
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("refuses a blank cap without a request", async () => {
    const user = userEvent.setup();
    mount(bot());
    await user.clear(screen.getByRole("textbox", { name: "Cap per task in dollars" }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(patch).not.toHaveBeenCalled();
  });
});
