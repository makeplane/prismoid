/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// THE tint recipe. Every status-colored surface in the app derives from this map - border at
// /25, wash at /10, text at full strength. Retyping these alphas anywhere else is how the five
// warning-alphas problem happened; add a variant here or don't add it.
export type Tone = "success" | "info" | "warning" | "danger" | "muted";

export const TONE_TINT: Record<Tone, string> = {
  success: "border-success/25 bg-success/10 text-success",
  info: "border-info/25 bg-info/10 text-info",
  warning: "border-warning/25 bg-warning/10 text-warning",
  danger: "border-destructive/25 bg-destructive/10 text-destructive",
  muted: "border-border text-muted-foreground",
};

export const TONE_TEXT: Record<Tone, string> = {
  success: "text-success",
  info: "text-info",
  warning: "text-warning",
  danger: "text-destructive",
  muted: "text-muted-foreground",
};

/** Card wash - a LARGER tinted surface (a gate, an escalation) than a chip. A card carries
 *  body text that must stay ink (DESIGN.md · Color: text is ink), so unlike TONE_TINT this
 *  tints ONLY the border + a lighter wash - never the text; the leading icon or heading wears
 *  the tone. Two card surfaces were hand-typing
 *  their own border/30 + bg/5 (and one at border/40) - the retyped-alpha drift this file
 *  exists to stop. One card-wash alpha, both schemes: border /30, wash /5. Pair with the
 *  `border` width utility. */
export const TONE_CARD: Record<Tone, string> = {
  success: "border-success/30 bg-success/5",
  info: "border-info/30 bg-info/5",
  warning: "border-warning/30 bg-warning/5",
  danger: "border-destructive/30 bg-destructive/5",
  muted: "border-border",
};

/** The categorical hue ramp (Collections v4). Kind-colors for USER DATA - a select option, a
 * chart series - never status (status is TONE_TINT above). ONE alpha law, both schemes:
 * wash /10, text full strength, border transparent. Callers index with `i % HUE_COUNT`.
 * Every string is a literal so Tailwind sees it. */
export const HUE_COUNT = 8;

/** The identity hash for the ramp: a NAME (an agent, a bot) lands on one hue, stably,
 *  everywhere it appears - the index law applied to strings (hash % HUE_COUNT). ONE copy
 *  on purpose: bot-avatar and the tree's agent chip each kept a byte-identical clone
 *  (one spelled the modulo `8`, one `HUE_COUNT`) - exactly the drift this file exists to
 *  stop. Identity, never status. */
export function nameHue(name: string): number {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return h % HUE_COUNT;
}

export const HUE_TINT: readonly string[] = [
  "border-transparent bg-hue-1/10 text-hue-1",
  "border-transparent bg-hue-2/10 text-hue-2",
  "border-transparent bg-hue-3/10 text-hue-3",
  "border-transparent bg-hue-4/10 text-hue-4",
  "border-transparent bg-hue-5/10 text-hue-5",
  "border-transparent bg-hue-6/10 text-hue-6",
  "border-transparent bg-hue-7/10 text-hue-7",
  "border-transparent bg-hue-8/10 text-hue-8",
];

/** Text-only hue classes - for SVG marks that paint with `currentColor` (chart lines,
 *  legend swatches). Same ramp, same index law as HUE_TINT/HUE_BG. */
export const HUE_TEXT: readonly string[] = [
  "text-hue-1",
  "text-hue-2",
  "text-hue-3",
  "text-hue-4",
  "text-hue-5",
  "text-hue-6",
  "text-hue-7",
  "text-hue-8",
];

/** Solid fills for shapes that carry their own label (gantt bars, chart marks). */
export const HUE_BG: readonly string[] = [
  "bg-hue-1",
  "bg-hue-2",
  "bg-hue-3",
  "bg-hue-4",
  "bg-hue-5",
  "bg-hue-6",
  "bg-hue-7",
  "bg-hue-8",
];
