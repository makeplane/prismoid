/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The project picker: opening it loads projects newest first, typing searches on the server,
// picking a row (or "No project") reports the choice, an unmatched name offers to create it,
// and a failed load says so.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "@prismoid/blocks/toast";
import { api } from "~/lib/api/client";
import { ProjectPicker, type ProjectRef } from "./project-picker";

const reply = (data: unknown) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });
const rows = (...names: string[]) => ({
  items: names.map((name) => ({ id: `id-${name}`, name })),
  next_cursor: null,
});

let get: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  get = vi.spyOn(api, "GET").mockImplementation(((_p: string, init: unknown) => {
    const q = (init as { params: { query: { q?: string } } }).params.query.q;
    return reply(q ? rows("Atlas launch") : rows("Alpha", "Beta"));
  }) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

function Harness({ onPick }: { onPick: (p: ProjectRef | null) => void }) {
  const [value, setValue] = useState<ProjectRef | null>({ id: "id-Alpha", name: "Alpha" });
  return (
    <ProjectPicker
      value={value}
      onChange={(p) => {
        setValue(p);
        onPick(p);
      }}
    />
  );
}

// The trigger's name carries the chosen project ("Project: Alpha"), or what it is for when unset.
const trigger = () => screen.getByRole("combobox", { name: /^Project: / });

describe("ProjectPicker", () => {
  it("shows the chosen project on its face and lists projects when opened", async () => {
    const user = userEvent.setup();
    render(<Harness onPick={vi.fn()} />);
    expect(trigger().textContent).toContain("Alpha");
    expect(trigger().getAttribute("aria-label")).toBe("Project: Alpha");
    await user.click(trigger());
    expect(await screen.findByRole("option", { name: "Beta" })).toBeDefined();
    expect(screen.getByRole("option", { name: "No project" })).toBeDefined();
  });

  it("unset, the face says Project - never an empty label slot", () => {
    // Regression: the unset picker left Propel's reserved value slot empty, a blank gap
    // between its icon and chevron.
    render(<ProjectPicker value={null} onChange={vi.fn()} />);
    expect(trigger().getAttribute("aria-label")).toBe("Project: where this task will live");
    expect(trigger().textContent).toBe("Project");
  });

  it("picking a project reports it, and No project clears it", async () => {
    const user = userEvent.setup();
    const onPick = vi.fn();
    render(<Harness onPick={onPick} />);
    await user.click(trigger());
    await user.click(await screen.findByRole("option", { name: "Beta" }));
    expect(onPick).toHaveBeenLastCalledWith({ id: "id-Beta", name: "Beta" });
    await user.click(trigger());
    await user.click(await screen.findByRole("option", { name: "No project" }));
    expect(onPick).toHaveBeenLastCalledWith(null);
  });

  it("typing searches by name on the server", async () => {
    const user = userEvent.setup();
    render(<Harness onPick={vi.fn()} />);
    await user.click(trigger());
    await screen.findByRole("option", { name: "Beta" });
    await user.type(screen.getByPlaceholderText("Search or create a project…"), "atl");
    expect(await screen.findByRole("option", { name: "Atlas launch" })).toBeDefined();
    expect(JSON.stringify(get.mock.calls.at(-1))).toContain('"q":"atl"');
  });

  it("a name nothing matches offers to create it, and creating picks the new project", async () => {
    const user = userEvent.setup();
    const onPick = vi.fn();
    const post = vi
      .spyOn(api, "POST")
      .mockImplementation((() => reply({ id: "new-1", name: "Zed" })) as never);
    get.mockImplementation((() => reply(rows())) as never);
    render(<Harness onPick={onPick} />);
    await user.click(trigger());
    await user.type(await screen.findByPlaceholderText("Search or create a project…"), "Zed");
    await user.click(await screen.findByRole("option", { name: /Create .Zed./ }));
    await waitFor(() => expect(onPick).toHaveBeenCalledWith({ id: "new-1", name: "Zed" }));
    expect(post.mock.calls[0][1]).toEqual({ body: { name: "Zed" } });
  });

  it("a failed load says so", async () => {
    const user = userEvent.setup();
    const err = vi.spyOn(toast, "error").mockImplementation((() => "") as never);
    get.mockImplementation((() => Promise.reject(new Error("down"))) as never);
    render(<Harness onPick={vi.fn()} />);
    await user.click(trigger());
    await waitFor(() => expect(err).toHaveBeenCalledWith("Couldn't load projects."));
  });
});
