/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** Pure width model for the table lens. Persisted widths live in `view.widths` - document
 * state, staged and revisioned like any cell edit; live widths exist only between pointerdown
 * and pointerup. Defaults are the px equivalents of the old COL_CLASS utilities. */

import type { CollectionField } from "~/lib/collection";

export const DEFAULT_COL_PX: Record<CollectionField["type"], number> = {
  text: 260,
  number: 112,
  date: 128,
  select: 144,
  multi_select: 176,
  checkbox: 80,
  url: 160,
  person: 176,
};

export const COL_MIN = 80;
export const COL_MAX = 800;

export function clampColW(px: number): number {
  return Math.min(COL_MAX, Math.max(COL_MIN, Math.round(px)));
}

/** live > persisted > type default. Non-finite persisted values (bad bytes survive parsing -
 * the zod façade only requires integers) fall through to the type default, never to NaN. */
export function resolveWidths(
  fields: CollectionField[],
  persisted: Record<string, number> | undefined,
  live: Record<string, number> = {},
): Record<string, number> {
  const out: Record<string, number> = {};
  for (const field of fields) {
    const liveW = live[field.key];
    const savedW = persisted?.[field.key];
    out[field.key] =
      liveW != null && Number.isFinite(liveW)
        ? clampColW(liveW)
        : savedW != null && Number.isFinite(savedW)
          ? clampColW(savedW)
          : DEFAULT_COL_PX[field.type];
  }
  return out;
}
