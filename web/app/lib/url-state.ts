/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// URL-borne UI state - the console's standing law: every slice an operator is looking at is
// addressable, shareable, and survives a refresh and the Back button.
//
// These two helpers existed as hand-copied blocks: the `set(key, value)` param writer in both
// ResourceList and TaskConsole, and the `?tab=` reader/writer verbatim in all three detail
// detail pages (users, projects, bots). Copies of URL logic drift in exactly the way that hurts -
// one screen pushes where another replaces, and Back stops meaning one thing.

import { useCallback } from "react";
import { useSearchParams } from "react-router";

export type UrlWriteMode = "replace" | "push";

/**
 * Write several search params as ONE history entry, preserving every other param the page owns.
 *
 * One entry is the whole reason this exists: a chip that means "the Tasks tab, filtered to
 * failed" is a single move, and writing the two params through two calls leaves the reader
 * pressing Back twice through a state they were never in.
 *
 * `replace` (the default) edits the current entry - a filter change is not a place you go Back
 * to, one keystroke at a time. Pass `push` for a state a reader would expect Back to undo
 * (opening a peek, entering a tab from a chip).
 *
 * An empty value DELETES the param: absent is how a URL says "default", and `?status=` is a
 * filter set to nothing, which is a different and wrong thing.
 */
export function useUrlParams(): (patch: Record<string, string>, mode?: UrlWriteMode) => void {
  const [, setParams] = useSearchParams();
  return useCallback(
    (patch, mode = "replace") =>
      setParams(
        (p) => {
          const next = new URLSearchParams(p);
          for (const [key, value] of Object.entries(patch)) {
            if (value) next.set(key, value);
            else next.delete(key);
          }
          return next;
        },
        { replace: mode === "replace" },
      ),
    [setParams],
  );
}

/** The one-param case, which is most of them. */
export function useUrlParam(): (key: string, value: string, mode?: UrlWriteMode) => void {
  const setParams = useUrlParams();
  return useCallback((key, value, mode) => setParams({ [key]: value }, mode), [setParams]);
}

/**
 * A tab strip whose selection lives in `?tab=`. The FIRST tab is the default and is written
 * as the ABSENCE of the param - a canonical URL for the default view, so `/users/<id>` and
 * `/users/<id>?tab=overview` are not two addresses for one page.
 *
 * An unknown `?tab=` (a stale link, a typo) falls back to the default rather than rendering
 * nothing. Returns the setter too, so a chip elsewhere on the page can switch tabs in place -
 * which is the whole point: the operator investigates here and never navigates away.
 */
export function useUrlTab<T extends string>(tabs: readonly T[]): [T, (next: string) => void] {
  const [params] = useSearchParams();
  const setParam = useUrlParam();
  const raw = params.get("tab") ?? "";
  const tab = (tabs as readonly string[]).includes(raw) ? (raw as T) : tabs[0];
  const setTab = useCallback(
    (next: string) => setParam("tab", next === tabs[0] ? "" : next),
    [setParam, tabs],
  );
  return [tab, setTab];
}

/**
 * Which nav entry the current path belongs to: the LONGEST `to` that prefixes it, so
 * `/users/<id>` lights "Users" and `/` (the landing) only ever matches itself exactly.
 *
 * Shared by the product sidebar and the console sidebar - two different applications with two
 * different nav arrays, but one definition of "you are here". The copy in the console drifted
 * out of a copy in the product, which is the only way this function is ever wrong.
 */
export function activeNavPath(pathname: string, tos: readonly string[]): string {
  let best = "";
  for (const to of tos) {
    const matches =
      to === "/" ? pathname === "/" : pathname === to || pathname.startsWith(to + "/");
    if (matches && to.length > best.length) best = to;
  }
  return best;
}
