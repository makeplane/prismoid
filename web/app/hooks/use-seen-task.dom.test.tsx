/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The task page marks the viewer's inbox items about the task read: when it opens shown, again
// when the stream brings news (a settled turn, a gate), once the view has been still, never twice
// for the same news, never from a hidden tab, and the sidebar's badge re-reads only when it moved
// and never over a load already running. A card in a bot's chat marks only on news, never on open.
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import { SEEN_SETTLE_MS, type SeenView, seenKey } from "~/lib/seen-task";
import { useSeenTask } from "./use-seen-task";

const mocks = vi.hoisted(() => ({
  revalidate: vi.fn(),
  state: { value: "idle" as "idle" | "loading" },
  badge: { value: 0 as number | undefined },
}));

vi.mock("react-router", async (orig) => ({
  ...(await orig<typeof import("react-router")>()),
  useRevalidator: () => ({ state: mocks.state.value, revalidate: mocks.revalidate }),
}));
vi.mock("~/hooks/use-inbox-unread", () => ({ useInboxUnread: () => mocks.badge.value }));

const read = (marked: number, unread = 0) =>
  Promise.resolve({
    data: { marked, unread },
    error: undefined,
    response: new Response(null, { status: 200 }),
  });

type Props = { view: SeenView };
const running = (over: Partial<SeenView> = {}): SeenView => ({
  turn: 1,
  status: "running",
  gates: [],
  ...over,
});
const start = (view = running(), onOpen = true) =>
  renderHook((p: Props) => useSeenTask("t1", seenKey(p.view), { onOpen }), {
    initialProps: { view },
  });
const settle = () => act(() => vi.advanceTimersByTimeAsync(SEEN_SETTLE_MS));

let post: ReturnType<typeof vi.spyOn>;
let visibility: DocumentVisibilityState = "visible";
const setVisibility = (state: DocumentVisibilityState) => {
  visibility = state;
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
};
const calls = () =>
  post.mock.calls.filter((c: unknown[]) => c[0] === "/v1/inbox/tasks/{task_id}/read");

beforeEach(() => {
  vi.useFakeTimers();
  visibility = "visible";
  mocks.badge.value = 0;
  mocks.state.value = "idle";
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
  post = vi.spyOn(api, "POST").mockImplementation((() => read(0)) as never);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  mocks.revalidate.mockReset();
});

describe("useSeenTask", () => {
  it("marks the task seen once the opened page is still", async () => {
    start();
    expect(calls()).toHaveLength(0);
    await settle();
    expect(calls()).toHaveLength(1);
    expect(calls()[0][1]).toEqual({ params: { path: { task_id: "t1" } } });
  });

  it("does not mark again for the same news", async () => {
    const hook = start();
    await settle();
    hook.rerender({ view: running() });
    await settle();
    expect(calls()).toHaveLength(1);
  });

  it("marks again when the stream brings news", async () => {
    const hook = start();
    await settle();
    hook.rerender({ view: running({ gates: [{ gate_id: "g1" }] }) });
    await settle();
    expect(calls()).toHaveLength(2);
    hook.rerender({ view: running({ status: "delivered" }) });
    await settle();
    expect(calls()).toHaveLength(3);
  });

  it("a replay's burst of past statuses is one call", async () => {
    const hook = start(running({ status: "delivered" }));
    for (const status of ["planning", "running", "waiting", "running", "delivered"] as const) {
      hook.rerender({ view: running({ status }) });
      await act(() => vi.advanceTimersByTimeAsync(SEEN_SETTLE_MS / 4));
    }
    await settle();
    expect(calls()).toHaveLength(1);
  });

  it("waits while the tab is hidden and marks once it is shown", async () => {
    visibility = "hidden";
    const hook = start();
    hook.rerender({ view: running({ status: "delivered" }) });
    await settle();
    expect(calls()).toHaveLength(0);
    setVisibility("visible");
    await settle();
    expect(calls()).toHaveLength(1);
  });

  it("marks a turn that settled and moved on while the tab was hidden", async () => {
    const hook = start();
    await settle();
    setVisibility("hidden");
    hook.rerender({ view: running({ turn: 2 }) }); // turn 1 delivered; a follow-up started turn 2
    setVisibility("visible");
    await settle();
    expect(calls()).toHaveLength(2);
  });

  it("re-reads the badge only when it moved", async () => {
    const hook = start();
    await settle();
    expect(mocks.revalidate).not.toHaveBeenCalled();
    post.mockImplementation((() => read(1)) as never);
    hook.rerender({ view: running({ status: "delivered" }) });
    await settle();
    expect(mocks.revalidate).toHaveBeenCalledTimes(1);
  });

  it("re-reads the badge after a gate answered here read its knock on the server", async () => {
    mocks.badge.value = 2;
    post.mockImplementation((() => read(0, 2)) as never);
    const hook = start(running({ gates: [{ gate_id: "g1" }] }));
    await settle();
    expect(mocks.revalidate).not.toHaveBeenCalled();
    post.mockImplementation((() => read(0, 1)) as never); // the answer read it; nothing left to read
    hook.rerender({ view: running() });
    await settle();
    expect(mocks.revalidate).toHaveBeenCalledTimes(1);
  });

  it("never restarts a load already running: the re-read waits for it to finish", async () => {
    mocks.state.value = "loading";
    post.mockImplementation((() => read(1)) as never);
    const hook = start();
    await settle();
    expect(mocks.revalidate).not.toHaveBeenCalled();
    mocks.state.value = "idle";
    hook.rerender({ view: running() });
    await act(() => Promise.resolve());
    expect(mocks.revalidate).toHaveBeenCalledTimes(1);
  });

  it("a view that does not report on open marks only when news comes", async () => {
    const hook = start(running(), false);
    await settle();
    expect(calls()).toHaveLength(0);
    hook.rerender({ view: running({ status: "delivered" }) });
    await settle();
    expect(calls()).toHaveLength(1);
  });

  it("a failed call is quiet and leaves the items for the next news", async () => {
    post.mockImplementation((() => Promise.reject(new Error("down"))) as never);
    const hook = start();
    await settle();
    hook.rerender({ view: running({ status: "delivered" }) });
    await settle();
    expect(calls()).toHaveLength(2);
    expect(mocks.revalidate).not.toHaveBeenCalled();
  });
});
