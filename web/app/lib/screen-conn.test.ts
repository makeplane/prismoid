/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The screen pane's connection, per task in this tab: a reopened pane is
// the same connection and still knows it claimed; another task is another; no storage (or one
// that throws) is a fresh connection each time, never an error.
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  freshPaneConn,
  holdPaneConn,
  LOCK_WAIT_MS,
  paneConn,
  rememberClaim,
  rememberedClaim,
} from "./screen-conn";

afterEach(() => vi.unstubAllGlobals());

describe("the pane's connection, per task and per tab", () => {
  class Store {
    data = new Map<string, string>();
    getItem(k: string) {
      return this.data.get(k) ?? null;
    }
    setItem(k: string, v: string) {
      this.data.set(k, v);
    }
    removeItem(k: string) {
      this.data.delete(k);
    }
  }

  it("a reopened pane keeps its connection; another task gets its own", () => {
    vi.stubGlobal("sessionStorage", new Store());
    const first = paneConn("t1");
    expect(first).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
    expect(paneConn("t1")).toBe(first);
    expect(paneConn("t2")).not.toBe(first);
  });

  it("remembers that this connection claimed, until it is told otherwise", () => {
    vi.stubGlobal("sessionStorage", new Store());
    const conn = paneConn("t1");
    expect(rememberedClaim("t1", conn)).toBe(false);
    rememberClaim("t1", conn, true);
    expect(rememberedClaim("t1", conn)).toBe(true);
    expect(rememberedClaim("t1", "someother01")).toBe(false);
    rememberClaim("t1", conn, false);
    expect(rememberedClaim("t1", conn)).toBe(false);
  });

  it("no storage (or one that throws) is a fresh connection each time, never an error", () => {
    vi.stubGlobal("sessionStorage", {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
      removeItem: () => {
        throw new Error("denied");
      },
    });
    const a = paneConn("t1");
    expect(a).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
    expect(paneConn("t1")).not.toBe(a);
    expect(() => rememberClaim("t1", a, true)).not.toThrow();
    expect(rememberedClaim("t1", a)).toBe(false);
    vi.stubGlobal("sessionStorage", undefined);
    expect(paneConn("t1")).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
  });
});

describe("one tab per connection (a duplicated tab copies sessionStorage, the connection with it)", () => {
  /** Web Locks, as one browser holds them: a name is granted to one request at a time. */
  class Locks {
    held = new Set<string>();
    waiting: Array<() => void> = [];
    request(name: string, opts: { signal?: AbortSignal }, cb: () => Promise<void>): Promise<void> {
      return new Promise((resolve, reject) => {
        const grant = () => {
          if (opts.signal?.aborted) return;
          this.held.add(name);
          void cb().then(() => {
            this.held.delete(name);
            const next = this.waiting.shift();
            next?.();
            resolve();
          });
        };
        opts.signal?.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        );
        if (this.held.has(name)) this.waiting.push(grant);
        else grant();
      });
    }
  }

  it("the first tab holds its connection while its pane is open", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("navigator", { locks: new Locks() });
    const held = vi.fn();
    const taken = vi.fn();
    const release = holdPaneConn("conn00001", held, taken);
    await vi.advanceTimersByTimeAsync(0);
    expect(held).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(LOCK_WAIT_MS * 2);
    expect(taken).not.toHaveBeenCalled();
    release();
    vi.useRealTimers();
  });

  it("a copy finds the connection held, and mints its own", async () => {
    vi.useFakeTimers();
    const locks = new Locks();
    vi.stubGlobal("navigator", { locks });
    const releaseFirst = holdPaneConn(
      "conn00001",
      () => {},
      () => {},
    );
    await vi.advanceTimersByTimeAsync(0);
    const held = vi.fn();
    const taken = vi.fn();
    holdPaneConn("conn00001", held, taken);
    await vi.advanceTimersByTimeAsync(LOCK_WAIT_MS);
    expect(taken).toHaveBeenCalledOnce();
    expect(held).not.toHaveBeenCalled();
    releaseFirst();
    vi.useRealTimers();
  });

  it("a pane closed and reopened at once (React's double effect) gets its connection back", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("navigator", { locks: new Locks() });
    holdPaneConn(
      "conn00001",
      () => {},
      () => {},
    )();
    const held = vi.fn();
    const taken = vi.fn();
    holdPaneConn("conn00001", held, taken);
    await vi.advanceTimersByTimeAsync(LOCK_WAIT_MS * 2);
    expect(held).toHaveBeenCalledOnce();
    expect(taken).not.toHaveBeenCalled();
    vi.useRealTimers();
  });

  it("without Web Locks the connection is simply this tab's", () => {
    vi.stubGlobal("navigator", {});
    const held = vi.fn();
    holdPaneConn("conn00001", held, () => {});
    expect(held).toHaveBeenCalledOnce();
  });

  it("a fresh connection replaces the copied one in this tab only", () => {
    const store = new Map<string, string>();
    vi.stubGlobal("sessionStorage", {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => store.set(k, v),
      removeItem: (k: string) => store.delete(k),
    });
    const copied = paneConn("t1");
    const fresh = freshPaneConn("t1");
    expect(fresh).not.toBe(copied);
    expect(paneConn("t1")).toBe(fresh);
  });
});
