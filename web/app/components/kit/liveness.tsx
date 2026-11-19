/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The ONE liveness mark, round two ("I honestly hate city block", and the
// pixels agreed): the FADE TRIO, three dots rippling by opacity alone. No bounce, no
// rotation - the mark sits still beside tabular time/token figures instead of jittering
// against them. It replaces the arc at every product site (tail line, rows, peeks,
// LoadingState, StatusMark's working glyph); the arc spinner (@prismoid/blocks/spinner) stays
// for the composer. Motion is the one looping exception (DESIGN.md · Motion) and reduced
// motion falls STILL at readable opacity - animate-none alone would freeze the dots at
// 0.2 and the mark would read as absence.
import { cn } from "~/lib/utils";

const STEP_MS = 170;

export function Liveness({
  className,
  decorative = true,
}: {
  className?: string;
  /** Most rows carry their own words ("24s · 6.4k tok · Reading …") - the mark is
   *  decoration beside them. Pass false where it stands alone and must announce itself. */
  decorative?: boolean;
}) {
  return (
    <span
      className={cn("flex shrink-0 items-center justify-center gap-[14%]", className)}
      role={decorative ? undefined : "status"}
      aria-label={decorative ? undefined : "Working"}
      aria-hidden={decorative || undefined}
    >
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="animate-liveness aspect-square w-[22%] rounded-full bg-current opacity-20 motion-reduce:animate-none motion-reduce:opacity-60"
          style={{ animationDelay: `${i * STEP_MS}ms` }}
        />
      ))}
    </span>
  );
}
