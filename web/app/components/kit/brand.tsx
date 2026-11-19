/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The brand mark and the page loader that wears it. The mark is the one sign every header shows
// (the sidebar's, the operator console's, the sign-in card's); `PageLoading` is that same mark
// breathing over a sentence, for the moments a whole page is on its way (first paint, a change
// of screen). A panel or a row that loads inside a page keeps `LoadingState` and its fade trio:
// a logo in every list would be noise.
import type { ReactNode } from "react";
import { cn } from "~/lib/utils";

// The mark stands on the page with no tile behind it: the box keeps the old tile's footprint so
// headers do not shift, and the glyph fills it.
const SIZE = {
  sm: "size-7",
  lg: "size-10",
  xl: "size-11",
} as const;

/** The Prismoid mark: a prismoid seen from above its front edge, three faces in one ink. The
 *  faces differ only by opacity and the ink is the page's own text colour: near-black on a light
 *  page (top 95%, right 84%, left 75%), white on a dark one (top 100%, right 85%, left 70%). */
function PrismoidGlyph({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 726 552" fill="currentColor" className={className}>
      <path
        d="M363 0L531.056 80.7805L363 175.024L194.944 80.7805L363 0Z"
        className="[fill-opacity:0.95] dark:[fill-opacity:1]"
      />
      <path
        d="M179.026 102.715L349.556 198.346V552L0 403.902L179.026 102.715Z"
        className="[fill-opacity:0.75] dark:[fill-opacity:0.7]"
      />
      <path
        d="M376.444 198.346L546.974 102.715L726 403.902L376.444 552V198.346Z"
        className="[fill-opacity:0.84] dark:[fill-opacity:0.85]"
      />
    </svg>
  );
}

export function BrandMark({
  size = "sm",
  className,
}: {
  size?: keyof typeof SIZE;
  className?: string;
}) {
  return (
    <span
      aria-hidden
      className={cn(
        "text-foreground flex aspect-square shrink-0 items-center justify-center",
        SIZE[size],
        className,
      )}
    >
      <PrismoidGlyph className="size-full" />
    </span>
  );
}

/** PageLoading - a whole page on its way: the mark breathing by opacity, one sentence under it.
 *  Reduced motion holds the mark still at full strength. */
export function PageLoading({
  label = "Loading…",
  className,
}: {
  label?: ReactNode;
  className?: string;
}) {
  return (
    <div
      role="status"
      className={cn(
        "text-muted-foreground flex flex-1 flex-col items-center justify-center gap-3 py-24 text-body-sm-regular",
        className,
      )}
    >
      <BrandMark size="lg" className="animate-brand-breathe motion-reduce:animate-none" />
      {label}
    </div>
  );
}
