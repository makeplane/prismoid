/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// A bot's Memory tab, driven: rows say where they came from, search refetches by query, Add
// memory posts the chosen kind for this bot and prepends the row, edit saves the new text,
// and retire removes the row.
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import { MemoryTab } from "./memory-tab";

const ok = (data: unknown) => ({
  data,
  error: undefined,
  response: new Response(null, { status: 200 }),
});
const memory = (id: string, over: Record<string, unknown> = {}) => ({
  id,
  text: `Fact ${id}`,
  kind: "note",
  scope: "bot",
  source_task_id: null,
  project_id: null,
  use_count: 0,
  created_at: "2026-11-01T10:00:00Z",
  ...over,
});

let rows: unknown[];
let get: ReturnType<typeof vi.spyOn>;
let post: ReturnType<typeof vi.spyOn>;
let patch: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  rows = [];
  get = vi
    .spyOn(api, "GET")
    .mockImplementation((() => Promise.resolve(ok({ items: rows, next_cursor: null }))) as never);
  post = vi.spyOn(api, "POST").mockImplementation((() => Promise.resolve(ok({}))) as never);
  patch = vi.spyOn(api, "PATCH").mockImplementation((() => Promise.resolve(ok({}))) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

const mount = (projectId: string | null = null) =>
  render(
    <MemoryRouter>
      <MemoryTab name="scout" projectId={projectId} />
    </MemoryRouter>,
  );

describe("Memory list", () => {
  it("asks for this bot's memories and shows where each came from", async () => {
    rows = [
      memory("a", { source_task_id: "t1", use_count: 1 }),
      memory("b", { use_count: 3, kind: "preference" }),
    ];
    mount();
    expect(await screen.findByText("Fact a")).toBeTruthy();
    expect(get.mock.calls[0][1]).toMatchObject({
      params: { query: { scope: "bot", bot: "scout", limit: 30 } },
    });
    expect(screen.getByRole("link", { name: "a task" }).getAttribute("href")).toContain("t1");
    expect(screen.getByText(/used 1 time$/)).toBeTruthy();
    expect(screen.getByText(/added by hand/)).toBeTruthy();
    expect(screen.getByText(/used 3 times/)).toBeTruthy();
  });

  it("scopes the request to the bot's project when it has one", async () => {
    mount("proj-1");
    await screen.findByText("Nothing learned yet");
    expect((get.mock.calls[0][1] as { params: { query: object } }).params.query).toMatchObject({
      project_id: "proj-1",
    });
  });

  it("names a search with no matches", async () => {
    const user = userEvent.setup();
    mount();
    await user.type(await screen.findByPlaceholderText("Search memories…"), "qq");
    expect(await screen.findByText("No matches for “qq”")).toBeTruthy();
    const init = get.mock.calls[get.mock.calls.length - 1][1] as {
      params: { query: { q: string } };
    };
    expect(init.params.query.q).toBe("qq");
  });
});

describe("Changing memories", () => {
  it("adds a memory of the chosen kind and puts it on top", async () => {
    const user = userEvent.setup();
    rows = [memory("a")];
    post.mockResolvedValueOnce(
      ok(memory("new", { text: "Likes tables", kind: "preference" })) as never,
    );
    mount();
    await screen.findByText("Fact a");
    await user.click(screen.getByRole("button", { name: "Add memory" }));
    const add = screen.getAllByRole("button", { name: "Add memory" }).at(-1) as HTMLButtonElement;
    expect(add.disabled).toBe(true);
    await user.type(await screen.findByRole("textbox", { name: "Memory" }), " Likes tables ");
    await user.click(screen.getByRole("button", { name: "preference" }));
    await user.click(add);
    expect(post).toHaveBeenCalledWith("/v1/memories", {
      body: { scope: "bot", bot_name: "scout", kind: "preference", text: "Likes tables" },
    });
    const items = await screen.findAllByRole("listitem");
    expect(items[0].textContent).toContain("Likes tables");
  });

  it("edits a row in place and saves the new text", async () => {
    const user = userEvent.setup();
    rows = [memory("a")];
    patch.mockResolvedValueOnce(ok(memory("a", { text: "Fact A2" })) as never);
    mount();
    await screen.findByText("Fact a");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const input = screen.getByDisplayValue("Fact a");
    await user.clear(input);
    await user.type(input, "Fact A2{Enter}");
    expect(patch).toHaveBeenCalledWith("/v1/memories/{memory_id}", {
      params: { path: { memory_id: "a" } },
      body: { text: "Fact A2" },
    });
    expect(await screen.findByText("Fact A2")).toBeTruthy();
  });

  it("Escape abandons an edit without a request", async () => {
    const user = userEvent.setup();
    rows = [memory("a")];
    mount();
    await screen.findByText("Fact a");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.type(screen.getByDisplayValue("Fact a"), "x{Escape}");
    expect(patch).not.toHaveBeenCalled();
    expect(screen.getByText("Fact a")).toBeTruthy();
  });

  it("retires a memory and drops the row", async () => {
    const user = userEvent.setup();
    rows = [memory("a"), memory("b")];
    mount();
    const row = (await screen.findByText("Fact a")).closest("li") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Retire" }));
    expect(post).toHaveBeenCalledWith("/v1/memories/{memory_id}/retire", {
      params: { path: { memory_id: "a" } },
    });
    expect(screen.queryByText("Fact a")).toBeNull();
    expect(screen.getByText("Fact b")).toBeTruthy();
  });
});
