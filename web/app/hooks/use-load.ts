/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { type DependencyList, useEffect, useRef, useState } from "react";
import { startLoad, type LoadHandlers } from "~/lib/load";

export type LoadState<T> = {
  /** The last answer; a failure or a re-run keeps it (a refetch never blanks what is shown). */
  data: T | undefined;
  /** The last failure, cleared when a run starts or succeeds. */
  error: unknown;
  loading: boolean;
};

/** One read per change of `deps`, cancelled on unmount and on the next change: the hand-written
 *  `let cancelled = false` effect, once. `load` null means "not now" (nothing runs), so whatever
 *  decides that belongs in `deps`. The handlers are for callers that fold the answer into state
 *  of their own; they always see the latest render's closures, and never run for a cancelled
 *  read. The decision lives in lib/load.ts. */
export function useLoad<T>(
  load: ((signal: AbortSignal) => Promise<T>) | null,
  deps: DependencyList,
  handlers: LoadHandlers<T> = {},
): LoadState<T> {
  const [state, setState] = useState<LoadState<T>>({
    data: undefined,
    error: undefined,
    loading: load != null,
  });
  const loadRef = useRef(load);
  loadRef.current = load;
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;
  // The caller's `deps` are the contract, the way `useEffect`'s are.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    const run = loadRef.current;
    if (!run) {
      setState((s) => (s.loading ? { ...s, loading: false } : s));
      return;
    }
    setState((s) =>
      s.loading && s.error === undefined ? s : { ...s, loading: true, error: undefined },
    );
    return startLoad(run, {
      onData: (data) => {
        setState({ data, error: undefined, loading: false });
        handlersRef.current.onData?.(data);
      },
      onError: (error) => {
        setState((s) => ({ ...s, error, loading: false }));
        handlersRef.current.onError?.(error);
      },
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return state;
}
