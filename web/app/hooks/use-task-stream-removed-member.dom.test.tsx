/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// A ticket mint refused with 404 (the viewer was removed from the project) ends the stream, never retries.
// The task stream resumes: a return to a task folds the frames this tab already holds and asks
// the server only for what came after them, and the view it shows is the one a full replay shows.
// It opens with a single-use ticket, never the session token, and a drop reopens it with a fresh one.
import { act, cleanup, renderHook } from "@testing-library/react";
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

let post: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  FakeSource.opened = [];
  taskFrameLog.clear();
  vi.stubGlobal("EventSource", FakeSource);
  localStorage.setItem(STORAGE.token, "cs_test");
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("a removed member", () => {
  it("a 404 from the ticket mint stops reopening", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    post = vi.spyOn(api, "POST").mockImplementation((() =>
      Promise.resolve({
        data: undefined,
        error: { error: { code: "not_found", message: "no such task" } },
        response: new Response(null, { status: 404 }),
      })) as never);
    const { result } = renderHook(() => useTaskStream("t1", detail));
    for (let i = 0; i < 5; i++) {
      await act(async () => {
        await Promise.resolve();
      });
    }
    expect(post).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(120_000));
    // The refusal is final - one mint, no retries, and the page is told.
    expect(post).toHaveBeenCalledTimes(1);
    expect(result.current.gone).toBe(true);
  });

  it("a 503 from the ticket mint still backs off and retries", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    post = vi.spyOn(api, "POST").mockImplementation((() =>
      Promise.resolve({
        data: undefined,
        error: { error: { code: "unavailable", message: "restarting" } },
        response: new Response(null, { status: 503 }),
      })) as never);
    const { result } = renderHook(() => useTaskStream("t1", detail));
    await act(() => vi.advanceTimersByTimeAsync(4_000));
    expect(post.mock.calls.length).toBeGreaterThan(1);
    expect(result.current.gone).toBe(false);
  });
});
