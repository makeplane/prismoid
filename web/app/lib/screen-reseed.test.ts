/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createReseeder } from "./screen-reseed";

let state: "idle" | "loading";
const revalidate = vi.fn();
const fold = vi.fn();
const make = () => createReseeder({ start: 0, revalidate, state: () => state, fold });

beforeEach(() => {
  vi.useFakeTimers();
  state = "idle";
  revalidate.mockReset();
  fold.mockReset();
});
afterEach(() => vi.useRealTimers());

describe("the screen reseeder", () => {
  it("debounces a screen.state, revalidates, and folds the detail that lands after", () => {
    const r = make();
    r.version(1);
    r.version(2);
    vi.advanceTimersByTime(299);
    expect(revalidate).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(revalidate).toHaveBeenCalledTimes(1);
    r.revalidator("idle", "stale-detail"); // the detail from before our revalidation began
    expect(fold).not.toHaveBeenCalled();
    r.revalidator("loading", "stale-detail");
    r.revalidator("idle", "fresh-detail");
    expect(fold).toHaveBeenCalledWith("fresh-detail");
  });

  it("a revalidator state flip between the frame and the timer does not lose the reseed", () => {
    const r = make();
    r.version(1);
    r.revalidator("loading", "d0"); // a machine.* revalidation starts meanwhile...
    r.revalidator("idle", "d1"); // ...and ends
    vi.advanceTimersByTime(300);
    r.revalidator("loading", "d1");
    r.revalidator("idle", "d2");
    expect(fold).toHaveBeenCalledTimes(1);
    expect(fold).toHaveBeenCalledWith("d2");
  });

  it("a revalidation already in flight when the timer fires is superseded by ours", () => {
    const r = make();
    r.version(1);
    state = "loading";
    vi.advanceTimersByTime(300);
    r.revalidator("idle", "d");
    expect(fold).toHaveBeenCalledWith("d");
  });

  it("dispose cancels a pending timer", () => {
    const r = make();
    r.version(1);
    r.dispose();
    vi.advanceTimersByTime(1000);
    expect(revalidate).not.toHaveBeenCalled();
  });
});
