/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The shared task window: seeded rows paint without a fetch, a navigation refetches pins and
// recents, rename and pin move rows optimistically and roll back when the server refuses, and
// delete drops the row at once.
import { act, cleanup, render, renderHook, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { createMemoryRouter, RouterProvider, useNavigate } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { toast } from "@prismoid/blocks/toast";
import { api, ApiError } from "~/lib/api/client";
import {
  mergeTaskWindow,
  rowVerbs,
  TaskWindowProvider,
  toTaskRow,
  useTaskWindow,
  type TaskWindowRow,
  type TaskWindowSeed,
} from "./task-window";

const row = (id: string, at: string, over: Partial<TaskWindowRow> = {}): TaskWindowRow => ({
  id,
  label: `Task ${id}`,
  status: "delivered",
  pinned: false,
  createdAt: at,
  ...over,
});
const reply = (data: unknown) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });
const refuse = () =>
  Promise.resolve({
    data: undefined,
    error: { detail: "nope" },
    response: new Response(null, { status: 500 }),
  });

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("pure helpers", () => {
  it("rowVerbs offers curation only to the creator", () => {
    expect(rowVerbs({ creatorId: "me" }, "me")).toEqual({ rename: true, pin: true, delete: true });
    expect(rowVerbs({ creatorId: "other" }, "me").delete).toBe(false);
    expect(rowVerbs({ creatorId: "me" }, null).rename).toBe(false);
    expect(rowVerbs({}, "me").pin).toBe(false);
  });

  it("mergeTaskWindow is newest first, id-deduped, and ties break by id", () => {
    const merged = mergeTaskWindow(
      [row("a", "2026-10-01T00:00:00Z", { pinned: true })],
      [
        row("a", "2026-10-01T00:00:00Z"),
        row("b", "2026-10-02T00:00:00Z"),
        row("c", "2026-10-01T00:00:00Z"),
      ],
    );
    expect(merged.map((r) => r.id)).toEqual(["b", "c", "a"]);
    expect(merged.find((r) => r.id === "a")?.pinned).toBe(true);
  });

  it("toTaskRow prefers the title and reads pinned from pinned_at", () => {
    const r = toTaskRow({
      id: "x",
      goal: "the goal",
      title: "Named",
      status: "running",
      pinned_at: "2026-10-01T00:00:00Z",
      created_at: "2026-10-01T00:00:00Z",
      creator_id: "u",
      project_id: "p",
    } as never);
    expect(r).toMatchObject({ label: "Named", pinned: true, creatorId: "u", projectId: "p" });
  });
});

function hook(seed?: TaskWindowSeed) {
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <RouterProvider
      router={createMemoryRouter(
        [
          {
            path: "*",
            element: <TaskWindowProvider initial={seed}>{children}</TaskWindowProvider>,
          },
        ],
        { initialEntries: ["/"] },
      )}
    />
  );
  return renderHook(() => useTaskWindow(), { wrapper: Wrapper });
}

const SEED: TaskWindowSeed = {
  pins: [row("p1", "2026-10-03T00:00:00Z", { pinned: true })],
  recents: [row("r1", "2026-10-02T00:00:00Z"), row("r2", "2026-10-01T00:00:00Z")],
};

describe("TaskWindowProvider", () => {
  it("a seeded window paints at once and does not refetch on mount", async () => {
    const get = vi.spyOn(api, "GET");
    const { result } = hook(SEED);
    await waitFor(() => expect(result.current).not.toBeNull());
    expect(result.current.latest.map((r) => r.id)).toEqual(["p1", "r1", "r2"]);
    expect(get).not.toHaveBeenCalled();
  });

  it("without a seed it loads pinned and recent rows", async () => {
    vi.spyOn(api, "GET").mockImplementation(((
      _p: string,
      init: { params: { query: { pinned: boolean } } },
    ) =>
      reply({
        items: init.params.query.pinned
          ? [
              {
                id: "p",
                goal: "pinned one",
                status: "running",
                pinned_at: "x",
                created_at: "2026-10-02T00:00:00Z",
              },
            ]
          : [
              {
                id: "r",
                goal: "recent one",
                status: "running",
                created_at: "2026-10-01T00:00:00Z",
              },
            ],
      })) as never);
    const { result } = hook();
    await waitFor(() => expect(result.current.pins.map((r) => r.label)).toEqual(["pinned one"]));
    expect(result.current.recents.map((r) => r.label)).toEqual(["recent one"]);
  });

  it("rename patches the label, trims it, and PATCHes the title", async () => {
    const patch = vi.spyOn(api, "PATCH").mockImplementation((() => reply({})) as never);
    const { result } = hook(SEED);
    await act(() => result.current.rename("r1", "  Renamed  "));
    expect(result.current.recents.find((r) => r.id === "r1")?.label).toBe("Renamed");
    expect(patch.mock.calls[0][1]).toEqual({
      params: { path: { task_id: "r1" } },
      body: { title: "Renamed" },
    });
    await act(() => result.current.rename("r1", "Renamed"));
    expect(patch).toHaveBeenCalledTimes(1);
  });

  it("a refused rename rolls the label back and says why", async () => {
    const err = vi.spyOn(toast, "error").mockImplementation((() => "") as never);
    vi.spyOn(api, "PATCH").mockImplementation((() =>
      Promise.reject(new ApiError(403, "forbidden", "Only the creator can rename."))) as never);
    const { result } = hook(SEED);
    await act(() => result.current.rename("r1", "Mine"));
    expect(result.current.recents.find((r) => r.id === "r1")?.label).toBe("Task r1");
    expect(err).toHaveBeenCalledWith("Only the creator can rename.");
  });

  it("pinning moves the row into pins at its true position and PATCHes pinned", async () => {
    const patch = vi.spyOn(api, "PATCH").mockImplementation((() => reply({})) as never);
    const { result } = hook(SEED);
    await act(() => result.current.togglePin(SEED.recents[1]));
    expect(result.current.pins.map((r) => r.id)).toEqual(["p1", "r2"]);
    expect(result.current.recents.map((r) => r.id)).toEqual(["r1"]);
    expect(patch.mock.calls[0][1]).toMatchObject({ body: { pinned: true } });
  });

  it("a refused unpin puts the row back among the pins", async () => {
    vi.spyOn(toast, "error").mockImplementation((() => "") as never);
    vi.spyOn(api, "PATCH").mockImplementation((() => refuse()) as never);
    const { result } = hook(SEED);
    await act(() => result.current.togglePin(SEED.pins[0]));
    expect(result.current.pins.map((r) => r.id)).toEqual(["p1"]);
    expect(result.current.recents.map((r) => r.id)).toEqual(["r1", "r2"]);
  });

  it("delete drops the row at once and reports success", async () => {
    const del = vi.spyOn(api, "DELETE").mockImplementation((() => reply({})) as never);
    const { result } = hook(SEED);
    let ok = false;
    await act(async () => {
      ok = await result.current.remove("r1");
    });
    expect(ok).toBe(true);
    expect(result.current.latest.map((r) => r.id)).toEqual(["p1", "r2"]);
    expect(del.mock.calls[0][1]).toEqual({ params: { path: { task_id: "r1" } } });
  });

  it("a failed delete returns false and puts the row back where it was", async () => {
    vi.spyOn(toast, "error").mockImplementation((() => "") as never);
    vi.spyOn(api, "DELETE").mockImplementation((() => Promise.reject(new Error("x"))) as never);
    const { result } = hook(SEED);
    let ok = true;
    await act(async () => {
      ok = await result.current.remove("r1");
    });
    expect(ok).toBe(false);
    expect(result.current.recents.map((r) => r.id)).toEqual(["r1", "r2"]);
    await act(async () => {
      await result.current.remove("p1");
    });
    expect(result.current.pins.map((r) => r.id)).toEqual(["p1"]);
  });

  it("patch writes what a live surface knows into the matching row", async () => {
    const { result } = hook(SEED);
    act(() => result.current.patch("r2", { status: "running", label: "Live title" }));
    expect(result.current.recents.find((r) => r.id === "r2")).toMatchObject({
      status: "running",
      label: "Live title",
    });
  });

  it("a navigation refetches the window", async () => {
    const get = vi.spyOn(api, "GET").mockImplementation((() => reply({ items: [] })) as never);
    function Go() {
      const nav = useNavigate();
      return <button onClick={() => void nav("/elsewhere")}>go</button>;
    }
    const router = createMemoryRouter(
      [
        {
          path: "*",
          element: (
            <TaskWindowProvider initial={SEED}>
              <Go />
            </TaskWindowProvider>
          ),
        },
      ],
      { initialEntries: ["/"] },
    );
    render(<RouterProvider router={router} />);
    expect(get).not.toHaveBeenCalled();
    await act(async () => {
      await router.navigate("/elsewhere");
    });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("button", { name: "go" })).toBeDefined();
  });

  it("using the window outside its provider is a loud error", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => renderHook(() => useTaskWindow())).toThrow(/inside TaskWindowProvider/);
  });
});
