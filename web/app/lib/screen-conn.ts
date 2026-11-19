/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/**
 * The screen pane's connection id, per task in this tab. sessionStorage is
 * per tab, so a closed and reopened pane is the SAME connection (the relay's holder, the lease's
 * bound conn) while another tab is another; the claim is remembered with it, so a reopened pane
 * still knows the lease is its own. Every access may throw (a private window, blocked storage):
 * then each pane gets a fresh connection, as before, and nothing is remembered.
 *
 * A duplicated tab copies sessionStorage, and the connection with it: two tabs as one connection
 * would both drive. While its pane is open a tab holds a Web Lock named after its connection
 * (`holdPaneConn`); a tab that cannot have it - another tab of this browser does - mints its own
 * (`freshPaneConn`). Whether a tab drives is then the server's word (the relay's `role`).
 */
import { mintConn } from "~/lib/presence";

const CONN_RE = /^[A-Za-z0-9_-]{8,64}$/;
const connKey = (taskId: string) => `screen.conn.${taskId}`;
const claimKey = (taskId: string) => `screen.claim.${taskId}`;

function tabStore(): Storage | null {
  return typeof sessionStorage === "undefined" || !sessionStorage ? null : sessionStorage;
}

/** This tab's connection id for the task's pane: kept, or minted and kept. */
export function paneConn(taskId: string): string {
  try {
    const kept = tabStore()?.getItem(connKey(taskId));
    if (kept && CONN_RE.test(kept)) return kept;
  } catch {
    return mintConn();
  }
  return freshPaneConn(taskId);
}

/** A new connection id for the task's pane, kept in this tab (a duplicated tab's copy of the
 *  first's is replaced here only). */
export function freshPaneConn(taskId: string): string {
  const conn = mintConn();
  try {
    tabStore()?.setItem(connKey(taskId), conn);
  } catch {
    /* no storage: this pane's connection lives as long as it does */
  }
  return conn;
}

/** How long a tab waits for its connection's lock before it takes it as another tab's. */
export const LOCK_WAIT_MS = 1000;

/** Hold the pane's connection for this tab while its pane is open (a Web Lock named after it):
 *  `onHeld` once it is this tab's, `onTaken` when another tab of this browser holds it past
 *  LOCK_WAIT_MS (a duplicated tab). Waiting, not `ifAvailable`, so a pane closed and reopened at
 *  once (React's double effect) gets its own lock back. Without Web Locks it is simply this
 *  tab's. Returns the release. */
export function holdPaneConn(conn: string, onHeld: () => void, onTaken: () => void): () => void {
  const locks = typeof navigator === "undefined" ? undefined : navigator.locks;
  if (!locks) {
    onHeld();
    return () => {};
  }
  let closed = false;
  let release = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  const stop = new AbortController();
  const timer = setTimeout(() => stop.abort(), LOCK_WAIT_MS);
  locks
    .request(`screen.conn.${conn}`, { signal: stop.signal }, () => {
      clearTimeout(timer);
      if (!closed) onHeld();
      return held;
    })
    .catch(() => {
      if (!closed) onTaken();
    });
  return () => {
    closed = true;
    clearTimeout(timer);
    stop.abort();
    release();
  };
}

/** Remember (or forget) that this connection claimed control, so a reopened pane still knows the
 *  lease is ITS own and not another tab's. */
export function rememberClaim(taskId: string, conn: string, claimed: boolean): void {
  try {
    const store = tabStore();
    if (claimed) store?.setItem(claimKey(taskId), conn);
    else store?.removeItem(claimKey(taskId));
  } catch {
    /* no storage: a reopened pane offers "Take control here" */
  }
}

export function rememberedClaim(taskId: string, conn: string): boolean {
  try {
    return tabStore()?.getItem(claimKey(taskId)) === conn;
  } catch {
    return false;
  }
}
