/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { FolderKanban } from "lucide-react";
import { HUE_TINT } from "~/components/kit";
import { cn } from "~/lib/utils";
import { ICON_BY_SLUG } from "./catalog";
import { parseIcon } from "./value";

/** The project's face, rendered - one atom for every surface that shows a project (list rows,
 *  the room header, pickers). Wire in, pixels out: unknown slugs and prefixes fall back to
 *  the default folder, never crash. Hue tints the well via the categorical ramp (user data
 *  wears HUE_TINT, never status tones); neutral is the IconTile recipe. */

// Emoji run a size up from what the latin scale suggests: platform emoji draw small inside
// their em box, so matching the glyph sizes optically means overshooting the text size -
// otherwise the char floats high-left in the well (the header misalignment complaint).
const SIZES = {
  sm: { well: "size-6 rounded-sm", glyph: "size-3.5", emoji: "text-body-md-regular" },
  md: { well: "size-9 rounded-md", glyph: "size-4", emoji: "text-h4-regular" },
  // lg matches the room header's title+description block (~size-14): with the leading
  // mark top-aligned, one height means top AND bottom sit flush with the text.
  lg: { well: "size-14 rounded-lg", glyph: "size-7", emoji: "text-h1-regular" },
} as const;

export function ProjectIcon({
  icon,
  hue,
  size = "md",
  className,
}: {
  icon: string | null | undefined;
  hue: number | null | undefined;
  size?: keyof typeof SIZES;
  className?: string;
}) {
  const s = SIZES[size];
  const value = parseIcon(icon);
  const tint = hue == null ? "bg-muted text-muted-foreground" : HUE_TINT[hue % HUE_TINT.length];
  return (
    <span
      aria-hidden
      className={cn("flex shrink-0 items-center justify-center", s.well, tint, className)}
    >
      {value?.kind === "emoji" ? (
        <span className={cn("leading-none", s.emoji)}>{value.char}</span>
      ) : (
        (() => {
          const Glyph = (value?.kind === "lucide" && ICON_BY_SLUG.get(value.name)) || FolderKanban;
          return <Glyph className={s.glyph} />;
        })()
      )}
    </span>
  );
}
