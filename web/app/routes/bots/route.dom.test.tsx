/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The bots messenger, driven: one row per bot name fronted by its published head, search
// filters the rows, selecting a bot opens its pane with the tab in the URL, the pin toggles
// optimistically and rolls back on failure, and New bot posts only what was filled in.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TOPBAR_START_SLOT_ID } from "~/components/layout/topbar-slot";
import { api } from "~/lib/api/client";
import Bots, { heads, shouldRevalidate } from "./route";

type Props = ComponentProps<typeof Bots>;

const bot = (name: string, over: Record<string, unknown> = {}) => ({
  name,
  version: 1,
  status: "published",
  eval_status: "green",
  description: `${name} does a job. Load when asked.`,
  title: null,
  icon: null,
  icon_hue: null,
  pinned: false,
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
const fail = () => ({
  data: undefined,
  error: { detail: "nope" },
  response: new Response(null, { status: 500 }),
});

let get: ReturnType<typeof vi.spyOn>;
let post: ReturnType<typeof vi.spyOn>;
let put: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  get = vi
    .spyOn(api, "GET")
    .mockImplementation(((path: string) =>
      Promise.resolve(
        path.startsWith("/v1/bots/") ? ok([]) : ok({ items: [], next_cursor: null }),
      )) as never);
  post = vi
    .spyOn(api, "POST")
    .mockImplementation((() => Promise.resolve(ok({ name: "newbie" }))) as never);
  put = vi.spyOn(api, "PUT").mockImplementation((() => Promise.resolve(ok({}))) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

function mount(items: unknown[], selected: string | null = null, search = "") {
  const slot = document.createElement("div");
  slot.id = TOPBAR_START_SLOT_ID;
  document.body.append(slot);
  const router = createMemoryRouter(
    [
      {
        path: "/bots/:name?",
        element: <Bots {...({ loaderData: { items } } as unknown as Props)} />,
      },
    ],
    { initialEntries: [`/bots${selected ? `/${selected}` : ""}${search}`] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

describe("navigation inside the page", () => {
  it("opens another bot and another tab without fetching the list again", async () => {
    const loader = vi.fn(() => Promise.resolve({ items: [bot("alpha"), bot("beta")] }));
    const Page = () => (
      <Bots {...({ loaderData: { items: [bot("alpha"), bot("beta")] } } as unknown as Props)} />
    );
    const router = createMemoryRouter(
      [{ path: "/bots/:name?", loader, shouldRevalidate, element: <Page /> }],
      { initialEntries: ["/bots/alpha"] },
    );
    render(<RouterProvider router={router} />);
    await screen.findByRole("heading", { name: "alpha" });
    expect(loader).toHaveBeenCalledTimes(1);

    await router.navigate("/bots/beta");
    await router.navigate("/bots/beta?tab=runs");
    expect(await screen.findByRole("heading", { name: "beta" })).toBeTruthy();
    expect(loader).toHaveBeenCalledTimes(1);

    await router.revalidate();
    expect(loader).toHaveBeenCalledTimes(2);
  });
});

describe("heads", () => {
  it("fronts a name with its published version and notes a newer draft", () => {
    const rows = heads([
      bot("zed", { version: 1 }),
      bot("alpha", { version: 1 }),
      bot("alpha", { version: 2, status: "draft" }),
    ] as never);
    expect(rows.map((r) => r.head.name)).toEqual(["alpha", "zed"]);
    expect(rows[0].head.version).toBe(1);
    expect(rows[0].draftAhead?.version).toBe(2);
    expect(rows[1].draftAhead).toBeNull();
  });

  it("falls back to the newest version when nothing is published", () => {
    const rows = heads([
      bot("solo", { version: 1, status: "draft" }),
      bot("solo", { version: 2, status: "draft" }),
    ] as never);
    expect(rows[0].head.version).toBe(2);
    expect(rows[0].draftAhead).toBeNull();
  });
});

describe("Bot list", () => {
  it("lists each bot with a count and prompts to pick one", () => {
    mount([bot("alpha"), bot("beta", { pinned: true })]);
    expect(screen.getByRole("link", { name: /alpha/ }).getAttribute("href")).toBe("/bots/alpha");
    expect(screen.getByText("2 bots · 1 pinned to sidebar")).toBeTruthy();
    expect(screen.getByText("Pick a bot")).toBeTruthy();
  });

  it("names a miss while searching", async () => {
    const user = userEvent.setup();
    mount([bot("alpha")]);
    await user.type(await screen.findByPlaceholderText("Search bots…"), "zzz");
    expect(screen.getByText("No matches for “zzz”")).toBeTruthy();
    expect(screen.getByText("0 bots · 0 pinned to sidebar")).toBeTruthy();
  });

  it("a search that leaves the open bot out narrows the list and keeps its pane", async () => {
    const user = userEvent.setup();
    mount([bot("alpha"), bot("beta", { description: "Reviews contracts" })], "alpha", "?tab=runs");
    await screen.findByRole("heading", { name: "alpha" });
    await user.type(screen.getByPlaceholderText("Search bots…"), "contracts");
    expect(screen.queryByRole("link", { name: /alpha/ })).toBeNull();
    expect(screen.getByRole("heading", { name: "alpha" })).toBeTruthy();
  });

  it("filters rows by name or description as you type", async () => {
    const user = userEvent.setup();
    mount([bot("alpha"), bot("beta", { description: "Reviews contracts" })]);
    await user.type(await screen.findByPlaceholderText("Search bots…"), "contracts");
    expect(screen.queryByRole("link", { name: /alpha/ })).toBeNull();
    expect(screen.getByRole("link", { name: /beta/ })).toBeTruthy();
  });
});

describe("Pinning", () => {
  it("pins optimistically and sends the new state", async () => {
    const user = userEvent.setup();
    mount([bot("alpha")]);
    await user.click(screen.getByRole("button", { name: "Pin alpha to the sidebar" }));
    expect(put).toHaveBeenCalledWith("/v1/bots/{name}/pin", {
      params: { path: { name: "alpha" } },
      body: { pinned: true },
    });
    expect(
      await screen.findByRole("button", { name: "Unpin alpha from the sidebar" }),
    ).toBeTruthy();
  });

  it("rolls the pin back when the server refuses", async () => {
    const user = userEvent.setup();
    put.mockResolvedValueOnce(fail() as never);
    mount([bot("alpha")]);
    await user.click(screen.getByRole("button", { name: "Pin alpha to the sidebar" }));
    expect(await screen.findByRole("button", { name: "Pin alpha to the sidebar" })).toBeTruthy();
  });
});

describe("Bot pane", () => {
  it("shows the selected bot's identity, status and evals", () => {
    mount([bot("alpha", { version: 3, eval_status: "red" })], "alpha", "?tab=runs");
    expect(screen.getByRole("heading", { name: "alpha" })).toBeTruthy();
    expect(screen.getByText("evals red")).toBeTruthy();
    expect(screen.getByText("alpha does a job.")).toBeTruthy();
    expect(screen.getByRole("link", { name: /alpha/ }).getAttribute("aria-current")).toBe("true");
  });

  it("shows the draft ahead's status and evals in the header", () => {
    mount(
      [bot("alpha"), bot("alpha", { version: 2, status: "draft", eval_status: "untested" })],
      "alpha",
      "?tab=runs",
    );
    expect(screen.getByText(/v2/)).toBeTruthy();
    expect(screen.getByText("evals untested")).toBeTruthy();
  });

  it("switches tabs through the URL", async () => {
    const user = userEvent.setup();
    const router = mount([bot("alpha")], "alpha", "?tab=runs");
    await user.click(screen.getByRole("tab", { name: "Artifacts" }));
    expect(router.state.location.search).toBe("?tab=artifacts");
    await user.click(screen.getByRole("tab", { name: "Memory" }));
    expect(router.state.location.search).toBe("?tab=memory");
  });
});

describe("Creating a bot", () => {
  async function open() {
    const user = userEvent.setup();
    const router = mount([]);
    await user.click(await screen.findByRole("button", { name: "New bot" }));
    const name = await screen.findByRole("textbox", { name: "Name" });
    return { user, router, name };
  }

  it("keeps Create disabled without a name", async () => {
    await open();
    const create = screen.getByRole("button", { name: "Create bot" }) as HTMLButtonElement;
    expect(create.disabled).toBe(true);
  });

  it("posts just the name, asks the drafter, then opens Configure", async () => {
    const { user, router, name } = await open();
    await user.type(name, " scout ");
    await user.click(screen.getByRole("button", { name: "Create bot" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(post.mock.calls[0]).toEqual([
      "/v1/bots",
      { body: { name: "scout", can_ask_user: true } },
    ]);
    expect(post.mock.calls[1][0]).toBe("/v1/bots/{name}/draft");
    await waitFor(() => expect(router.state.location.pathname).toBe("/bots/newbie"));
    expect(router.state.location.search).toBe("?tab=configure");
  });

  it("sends instructions and routing, and skips the drafter when instructions exist", async () => {
    const { user, name } = await open();
    await user.type(name, "scout");
    await user.type(screen.getByRole("textbox", { name: /Instructions/ }), "Be brief");
    await user.click(screen.getByRole("button", { name: /Advanced/ }));
    await user.type(
      await screen.findByRole("textbox", { name: "What does this bot do?" }),
      "Scouts",
    );
    await user.type(screen.getByRole("textbox", { name: /When should it step in/ }), "a, b ,");
    await user.type(screen.getByRole("textbox", { name: "A goal it should win" }), "Find it");
    await user.click(screen.getByRole("button", { name: "Create bot" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post.mock.calls[0][1]).toEqual({
      body: {
        name: "scout",
        instructions: "Be brief",
        job: "Scouts",
        use_when: ["a", "b"],
        not_for: [],
        cases: [{ goal: "Find it", expect: "scout" }],
        can_ask_user: true,
      },
    });
  });

  it("stays put when the create fails", async () => {
    post.mockResolvedValueOnce(fail() as never);
    const { user, router, name } = await open();
    await user.type(name, "scout");
    await user.click(screen.getByRole("button", { name: "Create bot" }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(router.state.location.pathname).toBe("/bots");
    expect(post).toHaveBeenCalledTimes(1);
    expect(get).toBeDefined();
  });
});
