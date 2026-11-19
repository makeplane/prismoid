/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { CollectionField } from "~/lib/collection";
import { Cell } from "./cell";

/** One field value as a card/list chip: typed, labeled where a bare value would be mute, and
 * absent-silent - a card never shows an em-dash for a hole, and never a bare numeral (the "1 2"
 * bug). Numbers and checkboxes always carry their field label; dates carry it when the caller
 * says the card shows several date chips; selects stay Badges (the shell disambiguates); text,
 * url and person render bare but width-capped. */
export function FieldChip({
  field,
  value,
  labeled = false,
}: {
  field: CollectionField;
  value: unknown;
  labeled?: boolean;
}) {
  const absent = value == null || value === "" || (Array.isArray(value) && value.length === 0);
  if (absent) return null;

  if (labeled || field.type === "number" || field.type === "checkbox") {
    return (
      <span className="inline-flex min-w-0 max-w-full items-baseline gap-1">
        <span className="text-muted-foreground/80 shrink-0">{field.label}</span>
        <Cell field={field} value={value} />
      </span>
    );
  }
  if (field.type === "text" || field.type === "url" || field.type === "person") {
    // The inline-flex wrapper blockifies Cell's inner truncate span so it actually clips.
    return (
      <span className="inline-flex max-w-40 min-w-0">
        <Cell field={field} value={value} />
      </span>
    );
  }
  return (
    <span className="inline-flex max-w-full min-w-0">
      <Cell field={field} value={value} />
    </span>
  );
}

/** The chip row shared by board cards, gallery cards, and list rows: same wrap, same rhythm. */
export function FieldChipRow({
  fields,
  cells,
  className = "",
}: {
  fields: CollectionField[];
  cells: Record<string, unknown>;
  className?: string;
}) {
  if (fields.length === 0) return null;
  const dates = fields.filter((f) => f.type === "date").length;
  return (
    <div
      className={`text-muted-foreground flex flex-wrap items-center gap-x-2 gap-y-1 text-caption-md-regular ${className}`}
    >
      {fields.map((f) => (
        <FieldChip
          key={f.key}
          field={f}
          value={cells[f.key]}
          labeled={f.type === "date" && dates >= 2}
        />
      ))}
    </div>
  );
}
