/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The Propel-aware class merge (ADR-074): tailwind-merge taught that text-body-sm-regular is a
// size, not a color, so `cn("text-muted-foreground", "text-caption-md-regular")` keeps both.
export { cn } from "@prismoid/blocks/cn";

/** "editor" → "Editor": the CSS `capitalize` a Propel Badge cannot take (it refuses className). */
export function capitalize(s: string) {
  return s.charAt(0).toUpperCase() + s.slice(1);
}
