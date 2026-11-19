/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { span } from "../core/date-math";

/** Pure math for bar move/resize. Used by BOTH the live preview and the commit, so what the
 * user sees during the drag is exactly what stages on release. */

export function snapDays(dxPx: number, dayPx: number): number {
  return Math.round(dxPx / dayPx);
}

/** 1-day floor on both edges: the start may move right only to span−1; the end may move left
 * only to 1−span. Lawful deltas pass through untouched. */
export function clampResizeDays(
  edge: "start" | "end",
  days: number,
  startMs: number,
  endMs: number,
): number {
  const s = span(startMs, endMs);
  return edge === "start" ? Math.min(days, s - 1) : Math.max(days, 1 - s);
}

export function previewRange(
  mode: "move" | "start" | "end",
  days: number,
  startMs: number,
  endMs: number,
): { startMs: number; endMs: number } {
  const DAY = 86_400_000;
  if (mode === "move") return { startMs: startMs + days * DAY, endMs: endMs + days * DAY };
  if (mode === "start") return { startMs: startMs + days * DAY, endMs };
  return { startMs, endMs: endMs + days * DAY };
}
