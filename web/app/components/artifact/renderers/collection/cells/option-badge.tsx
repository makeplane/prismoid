/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Badge, type BadgeVariant } from "@makeplane/propel/components/badge";
import { HUE_COUNT } from "~/components/kit";
import type { CollectionField } from "~/lib/collection";

/** value → hue index for select/multi_select, by option order (% 8). Deterministic and
 * document-stable; unknown values and non-hued types are null (neutral). */
export function optionHueIndex(field: CollectionField | undefined, value: unknown): number | null {
  if (!field || (field.type !== "select" && field.type !== "multi_select")) return null;
  const i = field.options.indexOf(String(value));
  return i >= 0 ? i % HUE_COUNT : null;
}

/** The categorical ramp (kit/tone HUE_TINT, hues 295 → 345) in Propel's badge palette. */
const HUE_BADGE: readonly BadgeVariant[] = [
  "purple",
  "indigo",
  "info",
  "emerald",
  "success",
  "yellow",
  "orange",
  "crimson",
];

/** The badge variant for one option value. checkbox: Yes → success, No → neutral.
 * Unknown/absent → neutral - which is also how the board's " none" bucket stays quiet. */
export function optionVariant(field: CollectionField | undefined, value: unknown): BadgeVariant {
  if (field?.type === "checkbox") {
    return value === true || value === "Yes" ? "success" : "neutral";
  }
  const i = optionHueIndex(field, value);
  return i == null ? "neutral" : HUE_BADGE[i];
}

/** THE option value chip - select cells, multi_select cells, board column pills, swimlane and
 * list section headers. One representation of an option everywhere it appears. */
export function OptionBadge({
  field,
  value,
}: {
  field: CollectionField | undefined;
  value: unknown;
}) {
  const text = value == null ? "" : String(value);
  if (!text) return null;
  return (
    <span className="inline-flex max-w-full overflow-hidden">
      <Badge variant={optionVariant(field, text)} size="xs" label={text} />
    </span>
  );
}
