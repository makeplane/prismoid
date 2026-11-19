/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Input, InputGroup } from "@makeplane/propel/components/input";
import { cellNumber, cellString, type CollectionField } from "~/lib/collection";

/** One cell, editable - the type-appropriate input, shared by the row peek and any future grid
 * editing. Every drag gesture's non-drag path lives here, which is the a11y floor. */
export function CellEditor({
  field,
  value,
  onChange,
}: {
  field: CollectionField;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  switch (field.type) {
    case "number":
      return (
        // tabular-nums inherits into the input; Propel takes no className.
        <div className="tabular-nums">
          <InputGroup size="lg">
            <Input
              size="lg"
              type="number"
              defaultValue={cellNumber(value) ?? ""}
              onBlur={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
            />
          </InputGroup>
        </div>
      );
    case "date":
      return (
        <InputGroup size="lg">
          <Input
            size="lg"
            type="date"
            defaultValue={typeof value === "string" ? value : ""}
            onChange={(e) => onChange(e.target.value || null)}
          />
        </InputGroup>
      );
    case "checkbox":
      return (
        <input
          type="checkbox"
          checked={value === true}
          onChange={(e) => onChange(e.target.checked)}
          aria-label={field.label}
          className="size-4"
        />
      );
    case "select":
      return (
        <select
          value={cellString(value) ?? ""}
          onChange={(e) => onChange(e.target.value || null)}
          aria-label={field.label}
          className="border-input bg-input/20 dark:bg-input/30 h-8 w-full rounded-md border px-2 text-body-sm-regular"
        >
          <option value="">-</option>
          {field.options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      );
    case "multi_select":
      // Comma-separated entry; values outside options are refused server-side with a 422.
      return (
        <InputGroup size="lg">
          <Input
            size="lg"
            defaultValue={Array.isArray(value) ? value.join(", ") : ""}
            placeholder={field.options.join(", ")}
            onBlur={(e) => {
              const parts = e.target.value
                .split(",")
                .map((s) => s.trim())
                .filter(Boolean);
              onChange(parts.length ? parts : null);
            }}
          />
        </InputGroup>
      );
    default:
      return (
        <InputGroup size="lg">
          <Input
            size="lg"
            defaultValue={cellString(value) ?? ""}
            onBlur={(e) => onChange(e.target.value === "" ? null : e.target.value)}
          />
        </InputGroup>
      );
  }
}
