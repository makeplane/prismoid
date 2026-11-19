/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// `screen.state` frames carry no order, so the page re-reads the task after each (debounced) and
// reseeds `view.screen` from `machine.control` - and only from a read that landed AFTER its own
// revalidation began, never a stale one. Pure of React: the route feeds it and holds one instance,
// so a revalidator that changes identity (or a machine.* revalidation racing ours) cannot cancel it.

export type Reseeder = {
  /** The reducer's `screenVersion` (every value after the first starts the debounce). */
  version(n: number): void;
  /** The revalidator's state and the detail the loader holds right now. */
  revalidator(state: "idle" | "loading", detail: unknown): void;
  dispose(): void;
};

export const RESEED_DEBOUNCE_MS = 300;

export function createReseeder(opts: {
  start: number;
  revalidate: () => void;
  state: () => "idle" | "loading";
  fold: (detail: never) => void;
}): Reseeder {
  let seen = opts.start;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let pending = false;
  let sawLoading = false;
  return {
    version(n) {
      if (n === seen) return;
      seen = n;
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        timer = null;
        // A revalidation in flight is superseded by ours, so its end is ours.
        pending = true;
        sawLoading = opts.state() === "loading";
        opts.revalidate();
      }, RESEED_DEBOUNCE_MS);
    },
    revalidator(state, detail) {
      if (!pending) return;
      if (state === "loading") sawLoading = true;
      else if (sawLoading) {
        pending = false;
        sawLoading = false;
        opts.fold(detail as never);
      }
    },
    dispose() {
      if (timer) clearTimeout(timer);
      timer = null;
    },
  };
}
