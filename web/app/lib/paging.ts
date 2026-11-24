/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useCallback, useEffect, useRef, useState, type DependencyList } from "react";

/** One page of a keyset-paginated wire list (PageOut on the contract). */
export interface KeysetPage<T> {
  items: T[];
  next_cursor?: string | null;
}

/** A debounced mirror of `value` - the mount value lands immediately, edits settle after
 *  `ms`. The unit search boxes debounce with before refetching. */
export function useDebouncedValue<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    const t = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return settled;
}

/** Splice a fresh page one over the head of a loaded list. Everything at or newer than the
 *  page's oldest row is replaced wholesale; the loaded tail (strictly older) survives, minus
 *  any row the head also carries. Newest-first: an empty page one means an empty list. */
export function spliceHead<T extends { id: string }>(
  current: T[],
  pageOne: T[],
  orderKey: (item: T) => string,
): T[] {
  if (pageOne.length === 0) return [];
  const boundary = orderKey(pageOne[pageOne.length - 1]);
  const headIds = new Set(pageOne.map((t) => t.id));
  const tail = current.filter((t) => orderKey(t) < boundary && !headIds.has(t.id));
  return [...pageOne, ...tail];
}

/** Apply a local mutation to a loaded list. `null` means "not loaded yet" - there is nothing
 *  to patch, and a mutation must not conjure a list the fetch hasn't delivered. */
export function mutateLoaded<T>(current: T[] | null, fn: (items: T[]) => T[]): T[] | null {
  return current === null ? null : fn(current);
}

/**
 * Keyset-paginated list state over any fetcher - the one pagination behaviour every list in
 * the app shares (keyset only, no OFFSET).
 *
 * - `deps` name the query's identity (search, filters): when they change, the list resets and
 *   page one refetches. A stale response from a superseded query is dropped, never rendered.
 * - `loadMore()` appends the next page, id-deduped.
 * - `refreshMs` keeps the HEAD live: page one is re-read and spliced over the loaded head at
 *   the keyset boundary, so already-loaded tail pages survive the poll. Status changes happen
 *   to recent tasks, and recent is exactly what page one holds (newest-first). `refresh()` is
 *   the same re-read on demand: after a mutation whose answer carries no row to `mutate` in.
 * - `initial` seeds from a route loader so hydration paints data, not a spinner.
 */
export function useKeysetList<T extends { id: string }>(opts: {
  fetchPage: (cursor: string | null) => Promise<KeysetPage<T>>;
  deps: DependencyList;
  /** The keyset sort key (descending) - `created_at` everywhere in this app. */
  orderKey: (item: T) => string;
  refreshMs?: number;
  initial?: KeysetPage<T> | null;
  /** When false the list is dormant: no fetches, no refresh. Flipping true resets and
   *  fetches page one - how a hidden layout's lists stay cheap until shown. */
  enabled?: boolean;
}) {
  const { deps, refreshMs } = opts;
  const enabled = opts.enabled ?? true;
  const [items, setItems] = useState<T[] | null>(opts.initial?.items ?? null);
  const [cursor, setCursor] = useState<string | null>(opts.initial?.next_cursor ?? null);
  const [loadingMore, setLoadingMore] = useState(false);

  const fetchRef = useRef(opts.fetchPage);
  fetchRef.current = opts.fetchPage;
  const orderRef = useRef(opts.orderKey);
  orderRef.current = opts.orderKey;
  const cursorRef = useRef(cursor);
  cursorRef.current = cursor;

  const seq = useRef(0); // bumps on every reset; stale responses check it and drop
  const seeded = useRef(opts.initial != null);

  useEffect(() => {
    if (!enabled) return;
    if (seeded.current) {
      // The loader already fetched this exact query; the effect's first run would refetch it.
      seeded.current = false;
      return;
    }
    const token = ++seq.current;
    setItems(null);
    setCursor(null);
    fetchRef
      .current(null)
      .then((page) => {
        if (token !== seq.current) return;
        setItems(page.items);
        setCursor(page.next_cursor ?? null);
      })
      .catch(() => {
        if (token === seq.current) setItems((cur) => cur ?? []);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- deps ARE the dependency list
  }, [enabled, ...deps]);

  const refresh = useCallback(async () => {
    const token = seq.current;
    try {
      const page = await fetchRef.current(null);
      if (token !== seq.current) return; // the query changed mid-flight
      setItems((cur) => {
        if (cur === null) return page.items;
        const next = spliceHead(cur, page.items, orderRef.current);
        // With no surviving tail the list IS page one - adopt its cursor, or new rows
        // past the page boundary would be unreachable until a reload.
        if (next.length === page.items.length && cursorRef.current === null) {
          setCursor(page.next_cursor ?? null);
        }
        return next;
      });
    } catch {
      /* transient - keep what we have */
    }
  }, []);

  useEffect(() => {
    if (!refreshMs || !enabled) return;
    const id = window.setInterval(() => {
      if (!document.hidden) void refresh();
    }, refreshMs);
    return () => window.clearInterval(id);
  }, [refreshMs, enabled, refresh]);

  const loadMore = useCallback(async () => {
    const at = cursorRef.current;
    if (!at || loadingMore) return;
    const token = seq.current;
    setLoadingMore(true);
    try {
      const page = await fetchRef.current(at);
      if (token !== seq.current) return;
      setItems((cur) => {
        const seen = new Set((cur ?? []).map((t) => t.id));
        return [...(cur ?? []), ...page.items.filter((t) => !seen.has(t.id))];
      });
      setCursor(page.next_cursor ?? null);
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore]);

  /** Patch the loaded items in place - how a mutation's response lands without a refetch
   *  (edit ⇒ map, retire ⇒ filter, create ⇒ prepend). */
  const mutate = useCallback((fn: (items: T[]) => T[]) => {
    setItems((cur) => mutateLoaded(cur, fn));
  }, []);

  return { items, loadMore, loadingMore, hasMore: cursor !== null, mutate, refresh };
}
