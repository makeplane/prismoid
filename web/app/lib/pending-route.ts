/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Which page a click is heading to, while that page's loader is still out. The router keeps the
// old location until every loader settles, so the chrome that answers "where am I" (the
// sidebar's active row, the page area) would sit on the old page for the whole wait. This reads
// the destination off the navigation instead, and only for a real change of page: a search-param
// change, a revalidation or a form submit keeps the page that is on screen.

export type PendingNavigation = {
  state: "idle" | "loading" | "submitting";
  location?: { pathname: string; state?: unknown };
  formMethod?: string;
};

/** The pathname being loaded, or null when the page on screen is the one to show. */
export function pendingPath(nav: PendingNavigation, pathname: string): string | null {
  if (nav.state !== "loading" || nav.formMethod) return null;
  const next = nav.location?.pathname;
  return next && next !== pathname ? next : null;
}

/** Location state for a navigation whose source page is already showing its own wait (Home's
 *  ask and loader while the new task opens): the shell leaves that page up instead of swapping
 *  in the page loader. */
export const HOLD_PAGE = { holdPage: true } as const;

/** Whether the navigation in flight asked the shell to keep the page on screen. */
export function holdsPage(nav: PendingNavigation): boolean {
  const state = nav.location?.state;
  return typeof state === "object" && state !== null && "holdPage" in state && !!state.holdPage;
}

/** What the router hands a route's `shouldRevalidate`, cut to what the rule below reads. */
export type RevalidateArgs = {
  currentUrl: { pathname: string; search: string };
  nextUrl: { pathname: string; search: string };
  formMethod?: string;
  defaultShouldRevalidate: boolean;
};

/** A route's `shouldRevalidate` for a page that keeps a tab, a filter or an open peek in the
 *  query string. The router re-runs every loader on a search-param edit and holds the URL (so
 *  the tab or the peek) until they answer; the page on screen already has that data. An edit of
 *  the query string alone skips the loader; a change of page, a submit and an asked-for reload
 *  (the URL unchanged) keep the router's default. */
export function revalidateOnPageChange(args: RevalidateArgs): boolean {
  const { currentUrl, nextUrl, formMethod, defaultShouldRevalidate } = args;
  const queryOnly =
    currentUrl.pathname === nextUrl.pathname && currentUrl.search !== nextUrl.search;
  return queryOnly && !formMethod ? false : defaultShouldRevalidate;
}

/** The shell's `shouldRevalidate`. Nothing the shell loads depends on the URL, yet the router
 *  re-runs it whenever the query string differs between two locations, so leaving a page that
 *  holds a tab or a filter there (one bot's Configure tab for another bot) waited on the session
 *  check and every sidebar window. A differing query string alone is never a reason to reload
 *  the shell; a submit and an asked-for reload keep the router's default. */
export function revalidateShell(args: RevalidateArgs): boolean {
  const { currentUrl, nextUrl, formMethod, defaultShouldRevalidate } = args;
  return currentUrl.search !== nextUrl.search && !formMethod ? false : defaultShouldRevalidate;
}
