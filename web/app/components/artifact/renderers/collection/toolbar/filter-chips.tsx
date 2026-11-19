/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { X } from "lucide-react";
import { Input, InputGroup } from "@makeplane/propel/components/input";
import {
  cellNumber,
  fieldOf,
  type Collection,
  type CollectionField,
  type CollectionView,
  type FilterSpec,
} from "~/lib/collection";
import { OP_LABEL } from "./filter-menu";

/** The active filter/sort rules as removable pills (the AddView pill recipe, as spans - a
 * button cannot nest the inline value editors). Every change stages an update_view. */
export function FilterChips({
  data,
  view,
  onUpdateView,
}: {
  data: Collection;
  view: CollectionView;
  onUpdateView: (patch: Partial<CollectionView>) => void;
}) {
  if (view.filter.length === 0 && view.sort.length === 0) return null;

  const setSpec = (i: number, spec: FilterSpec) =>
    onUpdateView({ filter: view.filter.map((s, j) => (j === i ? spec : s)) });
  const dropSpec = (i: number) => onUpdateView({ filter: view.filter.filter((_, j) => j !== i) });
  const toggleSort = (i: number) =>
    onUpdateView({
      sort: view.sort.map((s, j) =>
        j === i
          ? { ...s, direction: s.direction === "asc" ? ("desc" as const) : ("asc" as const) }
          : s,
      ),
    });
  const dropSort = (i: number) => onUpdateView({ sort: view.sort.filter((_, j) => j !== i) });

  const pill =
    "bg-card inline-flex items-center gap-1.5 rounded-pill border px-2.5 py-1 text-caption-md-regular";

  return (
    <div className="flex flex-wrap items-center gap-2">
      {view.filter.map((spec, i) => {
        const field = fieldOf(data, spec.field);
        const complete = spec.op === "empty" || spec.op === "not_empty";
        return (
          <span key={`${i}-${spec.field}-${spec.op}`} className={pill}>
            <span className="text-muted-foreground">{field?.label ?? spec.field}</span>
            <span>{OP_LABEL[spec.op]}</span>
            {!complete && (
              <SpecValue
                field={field}
                spec={spec}
                onChange={(value) => setSpec(i, { ...spec, value })}
              />
            )}
            <button
              type="button"
              aria-label={`Remove filter on ${field?.label ?? spec.field}`}
              onClick={() => dropSpec(i)}
              className="text-muted-foreground hover:text-destructive"
            >
              <X className="size-3" />
            </button>
          </span>
        );
      })}
      {view.sort.map((s, i) => {
        const field = fieldOf(data, s.field);
        return (
          <span key={`sort-${s.field}`} className={pill}>
            <button
              type="button"
              onClick={() => toggleSort(i)}
              className="hover:text-foreground inline-flex items-center gap-1"
              aria-label={`Toggle sort direction for ${field?.label ?? s.field}`}
            >
              <span className="text-muted-foreground">{field?.label ?? s.field}</span>
              <span aria-hidden>{s.direction === "asc" ? "↑" : "↓"}</span>
            </button>
            <button
              type="button"
              aria-label={`Remove sort on ${field?.label ?? s.field}`}
              onClick={() => dropSort(i)}
              className="text-muted-foreground hover:text-destructive"
            >
              <X className="size-3" />
            </button>
          </span>
        );
      })}
    </div>
  );
}

/** The type-appropriate inline editor for one spec's value. Text-ish inputs commit on blur
 * (uncontrolled, remounted by the parent's key when the op changes); pickers commit on change. */
function SpecValue({
  field,
  spec,
  onChange,
}: {
  field: CollectionField | undefined;
  spec: FilterSpec;
  onChange: (value: FilterSpec["value"]) => void;
}) {
  const select =
    "border-input bg-input/20 dark:bg-input/30 h-6 rounded-md border px-1.5 text-caption-md-regular";
  switch (field?.type) {
    case "select":
      return (
        <select
          value={String(spec.value ?? "")}
          onChange={(e) => onChange(e.target.value)}
          aria-label="Filter value"
          className={select}
        >
          {field.options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      );
    case "checkbox":
      return (
        <select
          value={spec.value === false ? "No" : "Yes"}
          onChange={(e) => onChange(e.target.value === "Yes")}
          aria-label="Filter value"
          className={select}
        >
          <option>Yes</option>
          <option>No</option>
        </select>
      );
    case "number":
      return (
        <span className="w-20 tabular-nums">
          <InputGroup size="md">
            <Input
              size="md"
              type="number"
              defaultValue={cellNumber(spec.value) ?? ""}
              onBlur={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
              aria-label="Filter value"
            />
          </InputGroup>
        </span>
      );
    case "date":
      return (
        <span className="w-34">
          <InputGroup size="md">
            <Input
              size="md"
              type="date"
              value={typeof spec.value === "string" ? spec.value : ""}
              onChange={(e) => onChange(e.target.value || null)}
              aria-label="Filter value"
            />
          </InputGroup>
        </span>
      );
    default:
      return (
        <span className="w-24">
          <InputGroup size="md">
            <Input
              size="md"
              defaultValue={String(spec.value ?? "")}
              onBlur={(e) => onChange(e.target.value)}
              aria-label="Filter value"
            />
          </InputGroup>
        </span>
      );
  }
}
