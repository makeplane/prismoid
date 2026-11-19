/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Check } from "lucide-react";
import { initials } from "~/components/kit";
import { cellDateMs, cellNumber, cellString, type CollectionField } from "~/lib/collection";
import { OptionBadge } from "./option-badge";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** THE flow-row title type - gantt rail, table primary, list primary render this one string,
 * so a row's name never changes size or weight when the lens does. Cards (board/gallery) keep
 * their own two-line font-medium register; that contrast is anatomy, not drift. */
export const ROW_TITLE_CLASS = "truncate text-body-sm-regular leading-tight";

export function dateLabel(value: unknown): string | null {
  const ms = cellDateMs(value);
  if (ms == null) return null;
  const d = new Date(ms);
  return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}, ${d.getUTCFullYear()}`;
}

/** THE FieldType → pixels map - every layout renders cells through here, so a field type looks
 * the same on a board card, a table cell, and a list row. An uncoercible cell is an em-dash,
 * never a crash and never `[object Object]`. */
export function Cell({
  field,
  value,
  primary = false,
  primaryLines = 2,
}: {
  field: CollectionField;
  value: unknown;
  primary?: boolean;
  /** 1 = flow-row register (one line ON the lens grid); 2 = card register (clamped title). */
  primaryLines?: 1 | 2;
}) {
  const dash = <span className="text-muted-foreground">-</span>;
  if (value == null || value === "") return dash;

  switch (field.type) {
    case "number": {
      const n = cellNumber(value);
      if (n == null) return dash;
      return (
        <span className="tabular-nums">
          {field.unit && field.unit !== "%" ? `${field.unit}` : ""}
          {n.toLocaleString("en-US")}
          {field.unit === "%" ? "%" : ""}
        </span>
      );
    }
    case "date": {
      const label = dateLabel(value);
      return label ? <span className="tabular-nums whitespace-nowrap">{label}</span> : dash;
    }
    case "select": {
      const text = cellString(value);
      return text ? <OptionBadge field={field} value={text} /> : dash;
    }
    case "multi_select": {
      if (!Array.isArray(value) || value.length === 0) return dash;
      return (
        <span className="flex flex-wrap gap-1">
          {value.map((v) => (
            <OptionBadge key={String(v)} field={field} value={v} />
          ))}
        </span>
      );
    }
    case "checkbox":
      return value === true ? (
        <Check className="text-success size-4" aria-label="yes" />
      ) : (
        <span className="text-muted-foreground text-caption-md-regular">No</span>
      );
    case "url": {
      const href = cellString(value);
      if (!href) return dash;
      let host: string;
      try {
        host = new URL(href).hostname.replace(/^www\./, "");
      } catch {
        return <span className="truncate">{href}</span>;
      }
      return (
        <a
          href={href}
          target="_blank"
          rel="noreferrer noopener"
          className="text-primary truncate underline-offset-2 hover:underline"
        >
          {host}
        </a>
      );
    }
    case "person": {
      const name = cellString(value);
      if (!name) return dash;
      return (
        <span className="inline-flex min-w-0 items-center gap-1.5">
          <span
            aria-hidden
            className="bg-muted text-muted-foreground grid size-5 shrink-0 place-items-center rounded-full text-[0.625rem] font-medium"
          >
            {initials(name)}
          </span>
          <span className="truncate">{name}</span>
        </span>
      );
    }
    default: {
      const text = cellString(value);
      if (!text) return dash;
      // The primary is a BLOCK that clamps: truncate on an inline span only contributes
      // white-space:nowrap (overflow does not apply to inline boxes), which is exactly how card
      // titles once bled across board columns. line-clamp is a real clipping box; break-words
      // wraps unbroken tokens.
      return primary ? (
        primaryLines === 1 ? (
          <div className={ROW_TITLE_CLASS}>{text}</div>
        ) : (
          <div className="line-clamp-2 text-body-sm-regular font-medium break-words">{text}</div>
        )
      ) : (
        <span className="truncate">{text}</span>
      );
    }
  }
}
