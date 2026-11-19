/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The loader snapshot kept current: stamps, machine frames and a parked chip each ask for one
// re-read once the API answers (never while it is down), a reconnect rechecks the screens, and
// a screen.state frame reseeds the lease from the fresh read that follows it.
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { components } from "~/lib/api/types.gen";
import { emptyView, type TaskView } from "~/lib/task-view";
import { useLiveRefresh } from "./use-live-refresh";

type Detail = components["schemas"]["TaskDetailOut"];

const mocks = vi.hoisted(() => ({
  revalidate: vi.fn(),
  state: { value: "idle" as "idle" | "loading" },
  recheck: vi.fn(),
}));

vi.mock("react-router", async (orig) => ({
  ...(await orig<typeof import("react-router")>()),
  useRevalidator: () => ({ state: mocks.state.value, revalidate: mocks.revalidate }),
}));
vi.mock("~/hooks/use-screen", () => ({ recheckScreens: mocks.recheck }));

const settled = (over: Record<string, unknown> = {}) =>
  ({
    turn: 1,
    scale: "standard",
    deliverable: "spreadsheet",
    deliverable_format: null,
    machine: null,
    ...over,
  }) as unknown as Detail;

const running = (over: Partial<TaskView> = {}): TaskView => ({
  ...emptyView(),
  status: "running",
  turn: 1,
  ...over,
});

type Props = { detail: Detail; view: TaskView; connected: boolean };
let fold: ReturnType<typeof vi.fn<(fn: (v: TaskView) => TaskView) => void>>;
const start = (over: Partial<Props> = {}) =>
  renderHook((p: Props) => useLiveRefresh({ ...p, fleetThread: false, foldLocal: fold }), {
    initialProps: { detail: settled(), view: running(), connected: true, ...over },
  });

beforeEach(() => {
  fold = vi.fn();
  mocks.state.value = "idle";
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(new Response("ok", { status: 200 }))),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  mocks.revalidate.mockReset();
  mocks.recheck.mockReset();
});

describe("useLiveRefresh", () => {
  it("a settled snapshot asks for nothing", async () => {
    start();
    await new Promise((r) => setTimeout(r, 30));
    expect(mocks.revalidate).not.toHaveBeenCalled();
  });

  it("rechecks the screens when the stream comes back, not on first connect", () => {
    const { rerender } = start({ connected: true });
    expect(mocks.recheck).not.toHaveBeenCalled();
    rerender({ detail: settled(), view: running(), connected: false });
    rerender({ detail: settled(), view: running(), connected: true });
    expect(mocks.recheck).toHaveBeenCalledTimes(1);
  });

  it("a turn past the snapshot asks for one re-read, once per turn", async () => {
    const { rerender } = start({ view: running({ turn: 2 }) });
    await waitFor(() => expect(mocks.revalidate).toHaveBeenCalledTimes(1));
    rerender({ detail: settled(), view: running({ turn: 2, appliedSeq: 9 }), connected: true });
    await new Promise((r) => setTimeout(r, 30));
    expect(mocks.revalidate).toHaveBeenCalledTimes(1);
  });

  it("holds the re-read while the API is down", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new Error("down"))),
    );
    start({ view: running({ turn: 2 }) });
    await new Promise((r) => setTimeout(r, 50));
    expect(mocks.revalidate).not.toHaveBeenCalled();
  });

  it("a machine frame re-reads the task for the chip", async () => {
    const { rerender } = start();
    rerender({ detail: settled(), view: running({ machineVersion: 3 }), connected: true });
    await waitFor(() => expect(mocks.revalidate).toHaveBeenCalledTimes(1));
  });

  it("a chip still parked under a running task re-reads on the next record frame", async () => {
    const parked = settled({ machine: { state: "parked" } });
    const { rerender } = start({ detail: parked, view: running({ appliedSeq: 4 }) });
    await waitFor(() => expect(mocks.revalidate).toHaveBeenCalledTimes(1));
    rerender({ detail: parked, view: running({ appliedSeq: 5 }), connected: true });
    await waitFor(() => expect(mocks.revalidate).toHaveBeenCalledTimes(2));
  });

  it("an open gate means the parked chip is expected: no re-read", async () => {
    const parked = settled({ machine: { state: "parked" } });
    const gated = running({ gates: [{ gate_id: "g1" }] as TaskView["gates"] });
    start({ detail: parked, view: gated });
    await new Promise((r) => setTimeout(r, 30));
    expect(mocks.revalidate).not.toHaveBeenCalled();
  });

  it("a screen.state frame re-reads after the debounce and folds the lease from the fresh read", async () => {
    const { rerender } = start();
    rerender({ detail: settled(), view: running({ screenVersion: 1 }), connected: true });
    await waitFor(() => expect(mocks.revalidate).toHaveBeenCalledTimes(1), { timeout: 1000 });
    mocks.state.value = "loading";
    rerender({ detail: settled(), view: running({ screenVersion: 1 }), connected: true });
    expect(fold).not.toHaveBeenCalled();
    mocks.state.value = "idle";
    const fresh = settled({ machine: { control: null } });
    rerender({ detail: fresh, view: running({ screenVersion: 1 }), connected: true });
    expect(fold).toHaveBeenCalledTimes(1);
  });
});
