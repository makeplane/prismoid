/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { CSSProperties, ReactNode, Ref } from "react";
import { EmptyState } from "~/components/kit";
import { METRICS } from "./metrics";

// The lens grid, injected once - lenses consume `h-(--row-h)` / `h-(--strip-h)` / … so every
// lens shares one set of bones (metrics.ts is the single source; the gantt's pixel math
// imports the same numbers).
const LENS_GRID = {
  "--row-h": `${METRICS.row}px`,
  "--strip-h": `${METRICS.strip}px`,
  "--subrow-h": `${METRICS.subRow}px`,
  "--footer-h": `${METRICS.footer}px`,
} as CSSProperties;

/** The one surface every lens renders inside (Collections v3): a single ring, a single
 * scroller (visible scrollbars), and the record peek as an in-layout flex sibling - never an
 * overlay. Sticky rails inside the lenses resolve against THIS scroller, so nothing between
 * it and them may create overflow or transform.
 *
 * **Document flow scrolls like a document**: the shell hugs its FULL content
 * height - no viewport cap - so the page/pane scroller owns the one vertical scroll and a
 * board reads top-to-bottom exactly like prose. The internal scroller still pans
 * HORIZONTALLY (wide boards, gantts - the pane never scrolls sideways law needs the box),
 * but it never scrolls vertically in this mode. The old `max-h-[calc(100dvh-15rem)]` cap
 * was the scroll-inside-a-scroll the user hated. `fill` (an app-frame host that killed its
 * own scroll) keeps the bounded frame - no current kind uses it.
 *
 * `scroll-behavior: smooth` is BANNED on the scroller: the gantt compensates prepend-widening
 * with a same-frame scrollLeft write, and CSS smoothing would animate that write into a
 * visible lurch. Smooth motion is requested per-call via scrollTo options only. */
export function ViewShell({
  ariaLabel,
  empty,
  peek,
  scrollRef,
  fill,
  children,
}: {
  ariaLabel: string;
  empty?: { title: string; description?: string } | null;
  peek?: ReactNode;
  scrollRef?: Ref<HTMLDivElement>;
  /** App frame: the host killed its own scroll, so the shell stretches to fill the frame and
   * this scroller is THE page scroller. Default (document flow): hug content, cap at the
   * viewport so the surrounding page stays reachable. */
  fill?: boolean;
  children: ReactNode;
}) {
  return (
    // The artifact-object at rest (DESIGN Elevation): one ring, one soft shadow, an explicit
    // background - inside the reader Sheet the ground is bg-popover (≡ card in dark), which
    // would otherwise erase card/bucket contrast.
    <div
      className={`bg-card ring-foreground/10 overflow-hidden rounded-xl shadow-raised-100 ring-1 ${
        fill ? "flex min-h-0 flex-1 flex-col" : ""
      }`}
      style={LENS_GRID}
    >
      <div className={`flex items-stretch ${fill ? "min-h-0 flex-1" : ""}`}>
        <div
          ref={scrollRef}
          role="region"
          aria-label={ariaLabel}
          tabIndex={0}
          // Document flow: HORIZONTAL only, and the vertical wheel must pass THROUGH.
          // `overflow-auto overscroll-contain` here ate every wheel over the board: the
          // box had no vertical overflow left to scroll, and overscroll-contain then
          // blocked the chain to the page - cursor inside the board, page frozen; an inch
          // outside, scrolling fine. overscroll-x-contain stays so a
          // horizontal pan never triggers the browser's back gesture; the vertical axis
          // is hidden (content defines the height) and chains freely. Fill keeps both
          // axes - it IS the page scroller in that mode.
          className={`scrollbar-thin min-h-56 min-w-0 flex-1 ${
            fill
              ? "overflow-auto overscroll-contain"
              : "overscroll-x-contain overflow-x-auto overflow-y-hidden"
          }`}
        >
          {empty ? (
            <div className="p-6">
              <EmptyState compact title={empty.title} description={empty.description} />
            </div>
          ) : (
            children
          )}
        </div>
        {/* Document flow un-bounded the row, so the peek bounds ITSELF: anchored to the
            shell's top, capped near a viewport, its body scrolling inside - a long record
            must not become a panel as tall as the whole board. Fill keeps stretch. */}
        {peek && (
          <div className={fill ? "flex min-h-0" : "flex max-h-[80dvh] min-h-56 self-start"}>
            {peek}
          </div>
        )}
      </div>
    </div>
  );
}
