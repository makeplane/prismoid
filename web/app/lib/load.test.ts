/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it, vi } from "vitest";
import { startLoad } from "./load";

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

describe("startLoad", () => {
  it("hands the answer to onData", async () => {
    const onData = vi.fn();
    const onError = vi.fn();
    startLoad(async () => 7, { onData, onError });
    await flush();
    expect(onData).toHaveBeenCalledWith(7);
    expect(onError).not.toHaveBeenCalled();
  });

  it("hands a failure to onError", async () => {
    const onData = vi.fn();
    const onError = vi.fn();
    const boom = new Error("boom");
    startLoad(() => Promise.reject(boom), { onData, onError });
    await flush();
    expect(onError).toHaveBeenCalledWith(boom);
    expect(onData).not.toHaveBeenCalled();
  });

  it("drops an answer that settles after the cancel, and aborts the signal", async () => {
    const d = deferred<number>();
    let seen: AbortSignal | undefined;
    const onData = vi.fn();
    const cancel = startLoad(
      (signal) => {
        seen = signal;
        return d.promise;
      },
      { onData },
    );
    cancel();
    expect(seen?.aborted).toBe(true);
    d.resolve(1);
    await flush();
    expect(onData).not.toHaveBeenCalled();
  });

  it("drops a failure that settles after the cancel", async () => {
    const d = deferred<number>();
    const onError = vi.fn();
    const cancel = startLoad(() => d.promise, { onError });
    cancel();
    d.reject(new Error("late"));
    await flush();
    expect(onError).not.toHaveBeenCalled();
  });

  it("swallows a failure when no onError is given", async () => {
    startLoad(() => Promise.reject(new Error("quiet")), {});
    await flush();
  });
});
