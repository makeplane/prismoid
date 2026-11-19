/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The connector's face - the ProjectIcon grammar applied to services: a hue well from the
// categorical ramp (identity, never status: `nameHue(slug)` keeps one service one color on
// every surface) holding a monochrome brand mark, or the Plug glyph when no mark exists.
// Wire in, pixels out; unknown anything falls back, never crashes.

import { Plug } from "lucide-react";
import { HUE_TINT, nameHue } from "~/components/kit";
import { cn } from "~/lib/utils";
import { BRAND } from "./brand-icons";

const SIZES = {
  sm: { well: "size-6 rounded-sm", glyph: "size-3.5" },
  md: { well: "size-9 rounded-md", glyph: "size-4" },
  lg: { well: "size-12 rounded-lg", glyph: "size-6" },
} as const;

export function ConnectorIcon({
  slug,
  icon,
  size = "md",
  className,
}: {
  slug: string;
  /** The catalog's mark key when it aliases the slug; empty/undefined = the slug itself. */
  icon?: string | null;
  size?: keyof typeof SIZES;
  className?: string;
}) {
  const s = SIZES[size];
  const mark = BRAND[icon || slug];
  return (
    <span
      aria-hidden
      className={cn(
        "flex shrink-0 items-center justify-center",
        s.well,
        HUE_TINT[nameHue(slug)],
        className,
      )}
    >
      {mark ? (
        <svg viewBox="0 0 24 24" className={cn(s.glyph, "fill-current")} role="img">
          <path d={mark.path} />
        </svg>
      ) : (
        <Plug className={s.glyph} />
      )}
    </span>
  );
}
