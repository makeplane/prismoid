/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The keyset list hook and the debounced value: page one loads, loadMore appends without
// duplicates, a changed query drops the stale response, the poll splices the head, and a
// dormant list fetches nothing.
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useDebouncedValue, useKeysetList, type KeysetPage } from "./paging";

type Row = { id: string; at: string };
const row = (id: string, at: string): Row => ({ id, at });
const orderKey = (r: Row) => r.at;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("useDebouncedValue", () => {
  it("lands the mount value at once and settles edits after the delay", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(({ v }) => useDebouncedValue(v, 200), {
      initialProps: { v: "a" },
    });
    expect(result.current).toBe("a");
    rerender({ v: "ab" });
    expect(result.current).toBe("a");
    act(() => {
      vi.advanceTimersByTime(199);
    });
    expect(result.current).toBe("a");
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current).toBe("ab");
  });

  it("a newer edit restarts the wait", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(({ v }) => useDebouncedValue(v, 100), {
      initialProps: { v: "a" },
    });
    rerender({ v: "b" });
    act(() => {
      vi.advanceTimersByTime(60);
    });
    rerender({ v: "c" });
    act(() => {
      vi.advanceTimersByTime(60);
    });
    expect(result.current).toBe("a");
    act(() => {
      vi.advanceTimersByTime(40);
    });
    expect(result.current).toBe("c");
  });
});

type Props = { q: string; enabled?: boolean };

function setup(fetchPage: (c: string | null) => Promise<KeysetPage<Row>>, extra = {}) {
  return renderHook(
    (p: Props) =>
      useKeysetList<Row>({ fetchPage, deps: [p.q], orderKey, enabled: p.enabled, ...extra }),
    { initialProps: { q: "x" } as Props },
  );
}

const ids = (items: Row[] | null) => items?.map((r) => r.id);

describe("useKeysetList", () => {
  it("loads page one, then loadMore appends the next page without duplicates", async () => {
    const fetchPage = vi.fn(async (c: string | null) =>
      c === null
        ? { items: [row("b", "2"), row("a", "1")], next_cursor: "c1" }
        : { items: [row("a", "1"), row("z", "0")], next_cursor: null },
    );
    const { result } = setup(fetchPage);
    expect(result.current.items).toBeNull();
    await waitFor(() => expect(ids(result.current.items)).toEqual(["b", "a"]));
    expect(result.current.hasMore).toBe(true);
    await act(async () => {
      await result.current.loadMore();
    });
    expect(fetchPage).toHaveBeenLastCalledWith("c1");
    expect(ids(result.current.items)).toEqual(["b", "a", "z"]);
    expect(result.current.hasMore).toBe(false);
    await act(async () => {
      await result.current.loadMore();
    });
    expect(fetchPage).toHaveBeenCalledTimes(2);
  });

  it("seeds from the loader without refetching, and a changed query refetches", async () => {
    const fetchPage = vi.fn(async () => ({ items: [row("n", "9")], next_cursor: null }));
    const { result, rerender } = setup(fetchPage, {
      initial: { items: [row("s", "5")], next_cursor: "k" },
    });
    expect(ids(result.current.items)).toEqual(["s"]);
    expect(result.current.hasMore).toBe(true);
    expect(fetchPage).not.toHaveBeenCalled();
    rerender({ q: "y" });
    await waitFor(() => expect(ids(result.current.items)).toEqual(["n"]));
    expect(fetchPage).toHaveBeenCalledTimes(1);
  });

  it("drops the response of a superseded query", async () => {
    const pending: Array<(p: KeysetPage<Row>) => void> = [];
    const fetchPage = vi.fn(() => new Promise<KeysetPage<Row>>((res) => pending.push(res)));
    const { result, rerender } = setup(fetchPage);
    rerender({ q: "y" });
    await act(async () => {
      pending[1]({ items: [row("new", "2")] });
      await Promise.resolve();
    });
    await act(async () => {
      pending[0]({ items: [row("old", "1")] });
      await Promise.resolve();
    });
    expect(ids(result.current.items)).toEqual(["new"]);
  });

  it("a failed first fetch shows an empty list, not a spinner forever", async () => {
    const { result } = setup(() => Promise.reject(new Error("down")));
    await waitFor(() => expect(result.current.items).toEqual([]));
  });

  it("a dormant list fetches nothing until enabled", async () => {
    const fetchPage = vi.fn(async () => ({ items: [row("a", "1")] }));
    const { result, rerender } = setup(fetchPage);
    await waitFor(() => expect(result.current.items).not.toBeNull());
    rerender({ q: "x", enabled: false });
    fetchPage.mockClear();
    rerender({ q: "z", enabled: false });
    expect(fetchPage).not.toHaveBeenCalled();
    rerender({ q: "z", enabled: true });
    await waitFor(() => expect(fetchPage).toHaveBeenCalledTimes(1));
  });

  it("mutate patches the loaded rows in place", async () => {
    const fetchPage = vi.fn(async () => ({ items: [row("a", "1"), row("b", "2")] }));
    const { result } = setup(fetchPage);
    await waitFor(() => expect(result.current.items).not.toBeNull());
    act(() => result.current.mutate((items) => items.filter((r) => r.id !== "a")));
    expect(ids(result.current.items)).toEqual(["b"]);
  });

  it("the poll splices fresh head rows over the loaded list", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let n = 0;
    const fetchPage = vi.fn(async () => {
      n += 1;
      return n === 1
        ? { items: [row("b", "2"), row("a", "1")], next_cursor: null }
        : { items: [row("c", "3"), row("b", "2")], next_cursor: null };
    });
    const { result } = setup(fetchPage, { refreshMs: 1000 });
    await waitFor(() => expect(result.current.items?.length).toBe(2));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    await waitFor(() => expect(ids(result.current.items)).toEqual(["c", "b", "a"]));
  });
});
