/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// A refresh the page asks for on its own - a live event said the server's state moved (a
// `screen.state`, a `machine.*` frame, a turn's stamps) - never one a person asked for. A router
// revalidation whose loader fails swaps the whole page for the route's error screen, and live
// events arrive exactly when the server is in motion: an API restarting while someone drives the
// browser publishes the lease's grace on its way down, and every viewer's revalidation then hit a
// closed port. So this asks the API first and revalidates only once it answers; down,
// it keeps what is shown and asks again on a bounded backoff. Requests that arrive while one
// waits fold into it. Pure of React: the route holds one instance.

export const BACKGROUND_RETRY_MS = { first: 1000, max: 30_000 } as const;

export type BackgroundRefresh = {
  /** Revalidate as soon as the API answers. */
  request(): void;
  /** Forget a waiting retry (unmount); a later request starts afresh. */
  stop(): void;
};

export function createBackgroundRefresh(opts: {
  revalidate: () => void;
  reachable: () => Promise<boolean>;
}): BackgroundRefresh {
  let wait: number = BACKGROUND_RETRY_MS.first;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let asking = false;
  let generation = 0;

  const attempt = async () => {
    const mine = generation;
    asking = true;
    const up = await opts.reachable().catch(() => false);
    asking = false;
    if (mine !== generation) return;
    if (up) {
      wait = BACKGROUND_RETRY_MS.first;
      opts.revalidate();
      return;
    }
    timer = setTimeout(() => {
      timer = null;
      void attempt();
    }, wait);
    wait = Math.min(wait * 2, BACKGROUND_RETRY_MS.max);
  };

  return {
    request() {
      if (asking || timer) return; // the one in flight revalidates after this event too
      void attempt();
    },
    stop() {
      generation++;
      asking = false;
      if (timer) clearTimeout(timer);
      timer = null;
      wait = BACKGROUND_RETRY_MS.first;
    },
  };
}
