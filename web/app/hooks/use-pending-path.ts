/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// A thin shell over lib/pending-route.ts: the router's navigation in, "where is this click
// going" out. `useNavPath` is what the chrome reads so the sidebar lights the destination on
// the click; `usePendingPage` tells the shell when to swap the old page for the page loader.

import { useEffect, useState } from "react";
import { useLocation, useNavigation } from "react-router";
import { holdsPage, pendingPath } from "~/lib/pending-route";

/** A load that lands this fast swaps straight to the new page: a loader shown for a few frames
 *  reads as a flicker, not as speed. */
const LOADER_DELAY_MS = 120;

/** The pathname being loaded, or null. */
export function usePendingPath(): string | null {
  const navigation = useNavigation();
  const { pathname } = useLocation();
  return pendingPath(navigation, pathname);
}

/** The pathname the chrome should treat as current: the destination from the moment of the click. */
export function useNavPath(): string {
  const { pathname } = useLocation();
  return usePendingPath() ?? pathname;
}

/** True once a page change has been loading long enough to be worth showing. */
export function usePendingPage(): boolean {
  const navigation = useNavigation();
  // A page that is already showing its own wait keeps the screen (HOLD_PAGE).
  const pending = usePendingPath();
  const held = holdsPage(navigation);
  const [shown, setShown] = useState<string | null>(null);
  useEffect(() => {
    if (!pending) return setShown(null);
    const timer = setTimeout(() => setShown(pending), LOADER_DELAY_MS);
    return () => clearTimeout(timer);
  }, [pending]);
  return !held && pending !== null && shown === pending;
}
