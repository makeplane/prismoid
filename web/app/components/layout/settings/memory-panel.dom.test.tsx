/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The private memory panel: it lists the user's memories with where each came from, says when
// there are none, adds one from the dialog (Enter or the button, with the kind chosen), and edits
// and retires a row in place without a refetch.
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryRow } from "~/components/memory/memory-row";
import { api } from "~/lib/api/client";
import { MemoryPanel } from "./memory-panel";

const reply = (status: number, data: unknown, error?: unknown) =>
  Promise.resolve({ data, error, response: new Response(null, { status }) });

function memory(id: string, over: Partial<MemoryRow> = {}): MemoryRow {
  return {
    id,
    text: `memory ${id}`,
    kind: "note",
    scope: "user",
    status: "active",
    project_id: null,
    source_task_id: null,
    created_by_id: "u1",
    created_at: "2026-11-01T10:00:00Z",
    updated_at: "2026-11-01T10:00:00Z",
    last_used_at: null,
    use_count: 0,
    bot_name: null,
    skill_name: null,
    ...over,
  } as MemoryRow;
}

let rows: MemoryRow[];
let get: ReturnType<typeof vi.spyOn>;
let post: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  rows = [
    memory("m1", { text: "Prefers tables over prose", source_task_id: "t1", use_count: 3 }),
    memory("m2", { text: "Works in metric units", kind: "preference", use_count: 1 }),
  ];
  get = vi
    .spyOn(api, "GET")
    .mockImplementation((() => reply(200, { items: rows, next_cursor: null })) as never);
  post = vi.spyOn(api, "POST").mockImplementation(((path: string, init: { body?: unknown }) => {
    if (path === "/v1/memories") {
      const body = init.body as { kind: string; text: string };
      return reply(201, memory("m3", { text: body.text, kind: body.kind }));
    }
    return reply(200, {});
  }) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

function mount() {
  render(
    <MemoryRouter>
      <MemoryPanel />
    </MemoryRouter>,
  );
}

describe("MemoryPanel", () => {
  it("lists the user's memories with where each came from", async () => {
    mount();
    expect(await screen.findByText("Prefers tables over prose")).toBeDefined();
    expect(screen.getByRole("link", { name: "a task" }).getAttribute("href")).toBe("/tasks/t1");
    expect(screen.getByText(/used 3 times/)).toBeDefined();
    expect(screen.getByText(/added by you/)).toBeDefined();
    expect(screen.getByText(/used 1 time$/)).toBeDefined();
    expect(get.mock.calls[0]?.[1]).toMatchObject({
      params: { query: { scope: "user", limit: 30 } },
    });
  });

  it("says when nothing is remembered yet", async () => {
    rows = [];
    mount();
    expect(await screen.findByText("Nothing remembered yet")).toBeDefined();
  });

  it("adds a memory of the chosen kind from the dialog", async () => {
    const user = userEvent.setup();
    mount();
    await screen.findByText("Prefers tables over prose");
    await user.click(screen.getByRole("button", { name: "Add memory" }));
    const dialog = await screen.findByRole("dialog");
    const add = within(dialog).getByRole("button", { name: "Add memory" });
    expect(add.hasAttribute("disabled") || add.getAttribute("aria-disabled") === "true").toBe(true);

    await user.type(within(dialog).getByLabelText(/^Memory/), "  Writes in British English  ");
    // The chips capitalize in CSS; their names are the kinds themselves.
    await user.click(within(dialog).getByRole("button", { name: "instruction" }));
    await user.click(add);

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(post).toHaveBeenCalledWith("/v1/memories", {
      body: { scope: "user", kind: "instruction", text: "Writes in British English" },
    });
    expect(await screen.findByText("Writes in British English")).toBeDefined();
  });

  it("Enter in the field adds the memory", async () => {
    const user = userEvent.setup();
    mount();
    await screen.findByText("Prefers tables over prose");
    await user.click(screen.getByRole("button", { name: "Add memory" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/^Memory/), "Short answers{Enter}");
    expect(await screen.findByText("Short answers")).toBeDefined();
    expect(post.mock.calls[0]?.[1]).toEqual({
      body: { scope: "user", kind: "note", text: "Short answers" },
    });
  });

  it("edits a row in place and retires another", async () => {
    const patch = vi
      .spyOn(api, "PATCH")
      .mockImplementation((() =>
        reply(200, memory("m1", { text: "Prefers short tables" }))) as never);
    const user = userEvent.setup();
    mount();
    const first = (await screen.findByText("Prefers tables over prose")).closest("li")!;
    await user.click(within(first).getByRole("button", { name: "Edit" }));
    const input = within(first).getByRole("textbox");
    await user.clear(input);
    await user.type(input, "Prefers short tables{Enter}");
    expect(await screen.findByText("Prefers short tables")).toBeDefined();
    expect(patch).toHaveBeenCalledTimes(1);

    const second = screen.getByText("Works in metric units").closest("li")!;
    await user.click(within(second).getByRole("button", { name: "Retire" }));
    await waitFor(() => expect(screen.queryByText("Works in metric units")).toBeNull());
    expect(post).toHaveBeenCalledWith("/v1/memories/{memory_id}/retire", {
      params: { path: { memory_id: "m2" } },
    });
    // Both changes landed from their responses: the list was fetched once.
    expect(get).toHaveBeenCalledTimes(1);
  });
});
