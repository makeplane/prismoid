/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// A refresh the page asks for on its own, because a live event said the server's state moved: it
// revalidates only once the API answers, so a restart (or any blip) never trades the rendered page
// for the route's error screen. It keeps what is shown and asks again on a bounded backoff.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BACKGROUND_RETRY_MS, createBackgroundRefresh } from "./background-refresh";

let up: boolean;
const revalidate = vi.fn();
const reachable = vi.fn(async () => up);
const make = () => createBackgroundRefresh({ revalidate, reachable });

beforeEach(() => {
  vi.useFakeTimers();
  up = true;
  revalidate.mockReset();
  reachable.mockClear();
});
afterEach(() => vi.useRealTimers());

describe("a background refresh", () => {
  it("revalidates at once when the API answers", async () => {
    make().request();
    await vi.advanceTimersByTimeAsync(0);
    expect(revalidate).toHaveBeenCalledTimes(1);
  });

  it("never revalidates while the API is down: it keeps the page and asks again, backing off", async () => {
    up = false;
    make().request();
    await vi.advanceTimersByTimeAsync(0);
    expect(revalidate).not.toHaveBeenCalled();
    const waits = [1000, 2000, 4000, 8000, 16_000, 30_000, 30_000];
    for (const wait of waits) {
      const asked = reachable.mock.calls.length;
      await vi.advanceTimersByTimeAsync(wait - 1);
      expect(reachable).toHaveBeenCalledTimes(asked);
      await vi.advanceTimersByTimeAsync(1);
      expect(reachable).toHaveBeenCalledTimes(asked + 1);
      expect(revalidate).not.toHaveBeenCalled();
    }
    up = true;
    await vi.advanceTimersByTimeAsync(BACKGROUND_RETRY_MS.max);
    expect(revalidate).toHaveBeenCalledTimes(1);
  });

  it("folds the requests that arrive while one waits into that one revalidation", async () => {
    up = false;
    const r = make();
    r.request();
    await vi.advanceTimersByTimeAsync(0);
    r.request();
    r.request();
    up = true;
    await vi.advanceTimersByTimeAsync(BACKGROUND_RETRY_MS.first);
    expect(revalidate).toHaveBeenCalledTimes(1);
    expect(reachable).toHaveBeenCalledTimes(2);
  });

  it("starts the backoff over once the API has answered", async () => {
    up = false;
    const r = make();
    r.request();
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(1000 + 2000); // two failed retries
    up = true;
    await vi.advanceTimersByTimeAsync(4000);
    expect(revalidate).toHaveBeenCalledTimes(1);
    up = false;
    r.request();
    await vi.advanceTimersByTimeAsync(0);
    const asked = reachable.mock.calls.length;
    await vi.advanceTimersByTimeAsync(BACKGROUND_RETRY_MS.first);
    expect(reachable).toHaveBeenCalledTimes(asked + 1);
  });

  it("stopped, it asks nothing more and revalidates nothing", async () => {
    up = false;
    const r = make();
    r.request();
    await vi.advanceTimersByTimeAsync(0);
    r.stop();
    up = true;
    await vi.advanceTimersByTimeAsync(BACKGROUND_RETRY_MS.max * 2);
    expect(revalidate).not.toHaveBeenCalled();
    expect(reachable).toHaveBeenCalledTimes(1);
  });
});
