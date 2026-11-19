/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The one load-and-forget step behind `useLoad` (hooks/use-load.ts): start a read, hand its
// answer or its failure to the caller, and let a cancel make both go quiet. A component that
// unmounted, or whose inputs moved on, must never be handed a stale answer, so the cancel both
// aborts the request (the signal) and drops whatever settles after it. Pure and imperative so
// the rule is tested without a DOM.

export type LoadHandlers<T> = {
  onData?: (data: T) => void;
  onError?: (error: unknown) => void;
};

/** Start `load`; the returned function cancels it. After a cancel neither handler runs. */
export function startLoad<T>(
  load: (signal: AbortSignal) => Promise<T>,
  handlers: LoadHandlers<T>,
): () => void {
  const controller = new AbortController();
  let current = true;
  load(controller.signal).then(
    (data) => {
      if (current) handlers.onData?.(data);
    },
    (error: unknown) => {
      if (current) handlers.onError?.(error);
    },
  );
  return () => {
    current = false;
    controller.abort();
  };
}
