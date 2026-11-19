/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { ListFilter } from "lucide-react";
import { Button as ButtonElement } from "@makeplane/propel/elements/button";
import { Icon } from "@makeplane/propel/components/icon";
import {
  Menu,
  MenuContent,
  MenuGroup,
  MenuItem,
  MenuLabel,
  MenuSubmenu,
  MenuSubmenuContent,
  MenuSubmenuTrigger,
  MenuTrigger,
} from "@makeplane/propel/components/menu";
import type { Collection, CollectionField, CollectionView, FilterSpec } from "~/lib/collection";
import { TYPE_ICON } from "../cells/field-icon";

export const OP_LABEL: Record<FilterSpec["op"], string> = {
  eq: "is",
  ne: "is not",
  contains: "contains",
  gte: "≥",
  lte: "≤",
  empty: "is empty",
  not_empty: "is not empty",
};

const OPS_BY_TYPE: Record<CollectionField["type"], FilterSpec["op"][]> = {
  text: ["contains", "eq", "ne", "empty", "not_empty"],
  url: ["contains", "eq", "ne", "empty", "not_empty"],
  person: ["contains", "eq", "ne", "empty", "not_empty"],
  number: ["eq", "ne", "gte", "lte", "empty", "not_empty"],
  date: ["eq", "ne", "gte", "lte", "empty", "not_empty"],
  select: ["eq", "ne", "empty", "not_empty"],
  multi_select: ["contains", "empty", "not_empty"],
  checkbox: ["eq"],
};

/** Picking an op appends a spec with a type-appropriate default value; the chips row is where
 * the value gets refined. empty/not_empty are complete on click. */
function defaultValue(field: CollectionField, op: FilterSpec["op"]): FilterSpec["value"] {
  if (op === "empty" || op === "not_empty") return null;
  switch (field.type) {
    case "select":
      return field.options[0] ?? "";
    case "checkbox":
      return true;
    case "number":
      return 0;
    case "date":
      return new Date().toISOString().slice(0, 10);
    default:
      return "";
  }
}

export function FilterMenu({
  data,
  view,
  onUpdateView,
}: {
  data: Collection;
  view: CollectionView;
  onUpdateView: (patch: Partial<CollectionView>) => void;
}) {
  const add = (field: CollectionField, op: FilterSpec["op"]) =>
    onUpdateView({
      filter: [...view.filter, { field: field.key, op, value: defaultValue(field, op) }],
    });

  return (
    <Menu>
      {/* The element, not the ready-made: the trigger also carries the active-filter dot. */}
      <MenuTrigger
        render={<ButtonElement variant="secondary" size="sm" stretch="auto" />}
        aria-label="Filter"
      >
        <Icon icon={ListFilter} /> Filter
        <span
          aria-hidden
          className={`size-1.5 rounded-full ${view.filter.length > 0 ? "bg-primary" : "bg-transparent"}`}
        />
      </MenuTrigger>
      <MenuContent align="end">
        <MenuGroup>
          <MenuLabel>Add filter</MenuLabel>
          {data.fields.map((f) => (
            <MenuSubmenu key={f.key}>
              <MenuSubmenuTrigger
                label={f.label}
                icon={<Icon icon={TYPE_ICON[f.type]} tint="secondary" />}
              />
              <MenuSubmenuContent>
                {OPS_BY_TYPE[f.type].map((op) => (
                  <MenuItem key={op} label={OP_LABEL[op]} onClick={() => add(f, op)} />
                ))}
              </MenuSubmenuContent>
            </MenuSubmenu>
          ))}
        </MenuGroup>
      </MenuContent>
    </Menu>
  );
}
