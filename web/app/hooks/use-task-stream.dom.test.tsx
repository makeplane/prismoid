/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The task stream resumes: a return to a task folds the frames this tab already holds and asks
// the server only for what came after them, and the view it shows is the one a full replay shows.
// It opens with a single-use ticket, never the session token, and a drop reopens it with a fresh one.
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import { taskFrameLog } from "~/lib/frame-log";
import { STORAGE } from "~/lib/storage";
import { useTaskStream } from "./use-task-stream";

type Detail = Parameters<typeof useTaskStream>[1];

/** A stand-in for the browser's EventSource: records its URL and lets a test push frames. */
class FakeSource {
  static opened: FakeSource[] = [];
  listeners = new Map<string, (e: MessageEvent) => void>();
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {
    FakeSource.opened.push(this);
  }
  addEventListener(type: string, fn: (e: MessageEvent) => void) {
    this.listeners.set(type, fn);
  }
  close() {
    this.closed = true;
  }
  /** One frame as the browser hands it over: `id` set only on durable frames, then repeated. */
  push(type: string, body: Record<string, unknown>, lastEventId: string) {
    const e = new MessageEvent(type, { data: JSON.stringify({ type, ...body }), lastEventId });
    this.listeners.get(type)?.(e);
  }
}

const detail = {
  status: "running",
  goal: "Plan the offsite",
  cost: { total_usd: "0" },
  plan: { steps: [] },
  turn: 1,
} as unknown as Detail;

const since = (s: FakeSource) => new URL(s.url).searchParams.get("since");
const param = (s: FakeSource, name: string) => new URL(s.url).searchParams.get(name);

/** The n-th source, once the ticket mint before it has answered (a few microtasks; no timers, so
 *  it works under fake ones too). */
async function nth(n: number): Promise<FakeSource> {
  for (let i = 0; i < 20 && FakeSource.opened.length <= n; i++) {
    await act(async () => {
      await Promise.resolve();
    });
  }
  expect(FakeSource.opened.length).toBeGreaterThan(n);
  return FakeSource.opened[n]!;
}

let post: ReturnType<typeof vi.spyOn>;
let minted = 0;

beforeEach(() => {
  FakeSource.opened = [];
  minted = 0;
  taskFrameLog.clear();
  vi.stubGlobal("EventSource", FakeSource);
  localStorage.setItem(STORAGE.token, "cs_test");
  post = vi.spyOn(api, "POST").mockImplementation((() =>
    Promise.resolve({
      data: { ticket: `et_ticket_${++minted}`, expires_in: 30 },
      error: undefined,
      response: new Response(null, { status: 200 }),
    })) as never);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("useTaskStream", () => {
  it("a first visit asks from 0; a return asks after the newest durable frame it holds", async () => {
    const first = renderHook(() => useTaskStream("t1", detail));
    const s1 = await nth(0);
    expect(since(s1)).toBe("0");
    act(() => {
      s1.push("task.status", { seq: 1, status: "paused" }, "1");
      s1.push("step.narration", { seq: 2, markdown: "Outline ready." }, "2");
      // An id-less delta: the browser repeats the last id, so it is never kept.
      s1.push("step.delta", { step_id: "s1", text: "draft" }, "2");
    });
    await waitFor(() => expect(first.result.current.view.status).toBe("paused"));
    const shownBefore = first.result.current.view;
    first.unmount();
    expect(s1.closed).toBe(true);

    const back = renderHook(() => useTaskStream("t1", detail));
    const s2 = await nth(1);
    expect(since(s2)).toBe("2");
    // The view is already there before the server says anything, as it was.
    expect(back.result.current.view.status).toBe("paused");
    expect(back.result.current.view.appliedSeq).toBe(shownBefore.appliedSeq);
    expect(back.result.current.view.feed.length).toBeGreaterThan(0);
  });

  it("another task starts from 0, never from the first task's frames", async () => {
    const one = renderHook(() => useTaskStream("t1", detail));
    const s1 = await nth(0);
    act(() => s1.push("task.status", { seq: 5, status: "paused" }, "5"));
    one.unmount();
    renderHook(() => useTaskStream("t2", detail));
    expect(since(await nth(1))).toBe("0");
  });

  it("a different sign-in in the same tab starts from 0", async () => {
    const one = renderHook(() => useTaskStream("t1", detail));
    const s1 = await nth(0);
    act(() => s1.push("task.status", { seq: 5, status: "paused" }, "5"));
    one.unmount();
    localStorage.setItem(STORAGE.token, "cs_someone_else");
    renderHook(() => useTaskStream("t1", detail));
    expect(since(await nth(1))).toBe("0");
  });

  it("opens with a ticket minted for this task, and the session token never rides the URL", async () => {
    const { result } = renderHook(() => useTaskStream("t1", detail));
    const s1 = await nth(0);
    expect(post).toHaveBeenCalledWith("/v1/tasks/{task_id}/events/ticket", {
      params: { path: { task_id: "t1" } },
    });
    expect(new URL(s1.url).pathname).toBe("/v1/tasks/t1/events");
    expect(param(s1, "ticket")).toBe("et_ticket_1");
    expect(param(s1, "conn")).toBe(result.current.conn);
    expect(s1.url).not.toContain("token=");
    expect(s1.url).not.toContain("cs_test");
  });

  it("a drop closes the source and reopens it with a fresh ticket from the newest durable seq", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { result } = renderHook(() => useTaskStream("t1", detail));
    const s1 = await nth(0);
    act(() => s1.onopen?.());
    expect(result.current.connected).toBe(true);
    act(() => {
      s1.push("task.status", { seq: 3, status: "paused" }, "3");
      s1.onerror?.();
    });
    expect(s1.closed).toBe(true);
    expect(result.current.connected).toBe(false);
    expect(FakeSource.opened).toHaveLength(1); // not before the back-off
    await act(() => vi.advanceTimersByTimeAsync(1000));
    const s2 = await nth(1);
    expect(param(s2, "ticket")).toBe("et_ticket_2");
    expect(since(s2)).toBe("3");
  });

  it("an unmount while the ticket is being minted opens nothing", async () => {
    let answer: (v: unknown) => void = () => {};
    post.mockImplementation((() => new Promise((resolve) => (answer = resolve))) as never);
    const { unmount } = renderHook(() => useTaskStream("t1", detail));
    unmount();
    await act(async () => {
      answer({
        data: { ticket: "et_late", expires_in: 30 },
        error: undefined,
        response: new Response(null, { status: 200 }),
      });
      await Promise.resolve();
    });
    expect(FakeSource.opened).toHaveLength(0);
  });
});
