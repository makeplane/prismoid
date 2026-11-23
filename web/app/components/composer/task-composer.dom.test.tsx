/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The task-minting box: sending posts the goal with the chosen dials and lands on the new task
// (under its Project when it has one), the Options menu's settings shape the payload, a server
// refusal keeps the draft and names its reason, and the roster loader keeps the newest skills.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "@prismoid/blocks/toast";
import { api, ApiError } from "~/lib/api/client";
import { loadComposerOptions, TaskComposer, type ComposerOptions } from "./task-composer";

const reply = (data: unknown) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });

const options = (over: Partial<ComposerOptions> = {}): ComposerOptions => ({
  skills: [],
  fleetModels: [],
  connectors: [],
  catalog: null,
  bots: [],
  ...over,
});

let post: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  localStorage.clear();
  post = vi.spyOn(api, "POST").mockImplementation((() => reply({ task_id: "t-new" })) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
  document.body.innerHTML = "";
});

function Where() {
  return <p data-testid="where">{useLocation().pathname}</p>;
}

function mount(props: Partial<React.ComponentProps<typeof TaskComposer>> = {}, opts = options()) {
  const router = createMemoryRouter(
    [
      {
        path: "*",
        element: (
          <>
            <TaskComposer options={opts} {...props} />
            <Where />
          </>
        ),
      },
    ],
    { initialEntries: ["/"] },
  );
  return render(<RouterProvider router={router} />);
}

const box = () => screen.getByRole("textbox") as HTMLTextAreaElement;
const body = () => (post.mock.calls[0][1] as { body: Record<string, unknown> }).body;

describe("TaskComposer sending", () => {
  it("posts the goal with the default dials and opens the new task", async () => {
    const user = userEvent.setup();
    mount();
    await user.type(box(), "  compare trackers {Enter}");
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/tasks/t-new"));
    expect(post.mock.calls[0][0]).toBe("/v1/tasks");
    expect(body()).toMatchObject({
      goal: "compare trackers",
      mode: "standard",
      clarify: "auto",
      scale: "auto",
      direct: false,
      docker: false,
    });
    expect(typeof body().client_token).toBe("string");
    expect(body()).not.toHaveProperty("project_id");
    expect(body()).not.toHaveProperty("budget");
  });

  it("a Project page fixes the Project: no picker, and the task opens under it", async () => {
    const user = userEvent.setup();
    mount({ fixedProject: { id: "p1", name: "Launch" } });
    expect(screen.queryByRole("combobox", { name: /^Project: / })).toBeNull();
    await user.type(box(), "write notes{Enter}");
    await waitFor(() =>
      expect(screen.getByTestId("where").textContent).toBe("/projects/p1/tasks/t-new"),
    );
    expect(body().project_id).toBe("p1");
  });

  it("the landing remembers the Project it was handed and sends it", async () => {
    const user = userEvent.setup();
    mount({ initialProject: { id: "p9", name: "Nine" } });
    expect(screen.getByRole("combobox", { name: /^Project: / }).textContent).toContain("Nine");
    await user.type(box(), "go{Enter}");
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(body().project_id).toBe("p9");
  });

  it("a refused start keeps the draft and names the server's reason", async () => {
    const user = userEvent.setup();
    const err = vi.spyOn(toast, "error").mockImplementation((() => "") as never);
    post.mockImplementation((() =>
      Promise.reject(
        new ApiError(422, "unprocessable", "Invalid", undefined, { goal: "Goal is too vague." }),
      )) as never);
    mount();
    await user.type(box(), "do it{Enter}");
    await waitFor(() => expect(err).toHaveBeenCalledWith("Goal is too vague."));
    expect(box().value).toBe("do it");
    expect(screen.getByTestId("where").textContent).toBe("/");
  });

  it("a refused attachment is named in words about the file, not 'the request body is not valid'", async () => {
    const user = userEvent.setup();
    const err = vi.spyOn(toast, "error").mockImplementation((() => "") as never);
    post.mockImplementation((() =>
      Promise.reject(
        new ApiError(422, "validation_failed", "the request body is not valid", undefined, {
          "inputs.0.content": "String should have at most 7154348 characters",
        }),
      )) as never);
    mount();
    await user.upload(
      document.querySelector("input[type=file]") as HTMLInputElement,
      new File(["x"], "plan.pdf"),
    );
    await screen.findByText("plan.pdf");
    await user.type(box(), "read it{Enter}");
    await waitFor(() =>
      expect(err).toHaveBeenCalledWith("plan.pdf is over the 5 MB limit for one file."),
    );
    expect(screen.getByText("plan.pdf")).toBeTruthy();
  });

  it("a file sent with no goal is refused and stays attached", async () => {
    const user = userEvent.setup();
    const err = vi.spyOn(toast, "error").mockImplementation((() => "") as never);
    mount();
    await user.upload(
      document.querySelector("input[type=file]") as HTMLInputElement,
      new File(["x"], "plan.pdf"),
    );
    await screen.findByText("plan.pdf");
    await user.click(screen.getByRole("button", { name: "Submit" }));
    await waitFor(() => expect(err).toHaveBeenCalledWith("Describe the task to start."));
    expect(post).not.toHaveBeenCalled();
    expect(screen.getByText("plan.pdf")).toBeTruthy();
  });

  it("any other failure falls back to a plain message", async () => {
    const user = userEvent.setup();
    const err = vi.spyOn(toast, "error").mockImplementation((() => "") as never);
    post.mockImplementation((() => Promise.reject(new Error("boom"))) as never);
    mount();
    await user.type(box(), "do it{Enter}");
    await waitFor(() => expect(err).toHaveBeenCalledWith("Couldn't start the task."));
  });
});

type User = ReturnType<typeof userEvent.setup>;

// The menu is driven the keyboard way: happy-dom runs no transitions, and a submenu's panel
// takes no pointer until its open transition ends.
/** Open Options and the named setting's submenu. */
async function openSetting(user: User, name: RegExp) {
  await user.click(screen.getByRole("button", { name: "Options" }));
  (await screen.findByRole("menuitem", { name })).focus();
  await user.keyboard("{ArrowRight}");
}

/** Choose a row of the open submenu. */
async function choose(user: User, role: string, name: string | RegExp) {
  (await screen.findByRole(role, { name })).focus();
  await user.keyboard("{Enter}");
}

describe("TaskComposer options", () => {
  it("at rest the row is the Project picker and Options: no dial pills", () => {
    mount();
    expect(screen.getByRole("button", { name: "Options" })).toBeDefined();
    for (const name of [/Budget - a USD spend cap/, "Mode", "Size", "Deliverable", "Skills"])
      expect(screen.queryByRole("button", { name })).toBeNull();
    expect(screen.queryByRole("combobox", { name: /Mode|Size|Clarifying/ })).toBeNull();
  });

  it("a budget picked in Options shows as a pill and rides the payload", async () => {
    const user = userEvent.setup();
    mount();
    await openSetting(user, /^Budget/);
    await choose(user, "menuitemradio", "$5");
    await waitFor(() => expect(screen.getByText("≤ $5.00")).toBeDefined());
    await user.type(box(), "go{Enter}");
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(body().budget).toEqual({ usd: "5.00" });
  });

  it("Custom amount opens the budget panel; a typed amount commits on Enter", async () => {
    const user = userEvent.setup();
    mount();
    await openSetting(user, /^Budget/);
    await choose(user, "menuitem", "Custom amount…");
    await user.type(await screen.findByRole("textbox", { name: "Spend cap in USD" }), "3.5{Enter}");
    await waitFor(() => expect(screen.getByText("≤ $3.50")).toBeDefined());
  });

  it("Custom amount opens the panel even when the budget pill is already in the row", async () => {
    const user = userEvent.setup();
    mount();
    await openSetting(user, /^Budget/);
    await choose(user, "menuitemradio", "$5");
    await waitFor(() => expect(screen.getByText("≤ $5.00")).toBeDefined());
    await openSetting(user, /^Budget/);
    await choose(user, "menuitem", "Custom amount…");
    const field = await screen.findByRole("textbox", { name: "Spend cap in USD" });
    await user.clear(field);
    await user.type(field, "7{Enter}");
    await waitFor(() => expect(screen.getByText("≤ $7.00")).toBeDefined());
    // and clearing it afterwards leaves no empty pill behind
    await user.click(await screen.findByRole("button", { name: "Clear budget" }));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /Budget - a USD spend cap/ })).toBeNull(),
    );
  });

  it("a pill's clear button puts the setting back to its default", async () => {
    const user = userEvent.setup();
    mount();
    await openSetting(user, /^Budget/);
    await choose(user, "menuitemradio", "$2");
    await user.click(await screen.findByRole("button", { name: "Clear budget" }));
    await waitFor(() => expect(screen.queryByText("≤ $2.00")).toBeNull());
    await user.type(box(), "go{Enter}");
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(body()).not.toHaveProperty("budget");
  });

  it("mode and size picked in Options ride the payload", async () => {
    const user = userEvent.setup();
    mount();
    await openSetting(user, /^Size/);
    await choose(user, "menuitemradio", /^Brief/);
    await openSetting(user, /^Mode/);
    await choose(user, "menuitemradio", /^Fleet/);
    await user.type(box(), "go{Enter}");
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(body()).toMatchObject({ mode: "fleet", scale: "brief" });
  });

  it("pinned skills ride the payload, and the pill counts them", async () => {
    const user = userEvent.setup();
    const skills = [
      { name: "brief", description: "Write briefs", version: 1 },
      { name: "audit", description: "Audit things", version: 1 },
    ] as unknown as ComposerOptions["skills"];
    mount({}, options({ skills }));
    await openSetting(user, /^Skills/);
    await choose(user, "menuitemcheckbox", "brief");
    await user.keyboard("{Escape}{Escape}");
    expect(await screen.findByRole("button", { name: "Skills" })).toBeDefined();
    await user.type(box(), "go{Enter}");
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(body().skills).toEqual(["brief"]);
  });

  it("no skills means no Skills row in Options", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("button", { name: "Options" }));
    expect(await screen.findByRole("menuitem", { name: /^Budget/ })).toBeDefined();
    expect(screen.queryByRole("menuitem", { name: /^Skills/ })).toBeNull();
  });
});

describe("loadComposerOptions", () => {
  it("keeps the newest version of each skill, sorted by name", async () => {
    vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path === "/v1/skills")
        return reply([
          { name: "zeta", version: 1 },
          { name: "alpha", version: 1 },
          { name: "alpha", version: 3 },
        ]);
      if (path === "/v1/connectors") return reply({ items: [{ id: "c1" }] });
      if (path === "/v1/bots") return reply({ items: [{ name: "b" }] });
      return reply([]);
    }) as never);
    const out = await loadComposerOptions();
    expect(out.skills.map((s) => `${s.name}@${s.version}`)).toEqual(["alpha@3", "zeta@1"]);
    expect(out.connectors).toHaveLength(1);
    expect(out.bots).toHaveLength(1);
  });

  it("a failing roster is an empty one, never a thrown error", async () => {
    vi.spyOn(api, "GET").mockImplementation((() => Promise.reject(new Error("down"))) as never);
    const out = await loadComposerOptions();
    expect(out).toMatchObject({ skills: [], fleetModels: [], connectors: [], bots: [] });
  });
});
