/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Menu, MenuContent, MenuItem, MenuTrigger } from "@makeplane/propel/components/menu";
import { Plus } from "lucide-react";
import { ToggleChip } from "~/components/kit";
import { LAYOUTS, type Collection, type CollectionView, type Layout } from "~/lib/collection";

// What each layout needs before it can exist - a layout the data can't support is ABSENT
// from the menu, never disabled with an excuse (schema editing isn't live,
// so "needs a date field" is advice nobody can act on). Smart defaults (first groupable
// field, the date pair, the first url) keep "+ view" one click.
export const LAYOUT_LABEL: Record<Layout, string> = {
  table: "Table",
  board: "Board",
  list: "List",
  gallery: "Gallery",
  gantt: "Gantt",
  calendar: "Calendar",
};

export function AddView({
  data,
  onAdd,
}: {
  data: Collection;
  onAdd: (view: CollectionView) => void;
}) {
  const groupable = data.fields.find((f) => ["select", "person", "checkbox"].includes(f.type));
  const dates = data.fields.filter((f) => f.type === "date");
  const url = data.fields.find((f) => f.type === "url");

  const available: Layout[] = LAYOUTS.filter((layout) => {
    if (layout === "board") return !!groupable;
    if (layout === "gantt" || layout === "calendar") return dates.length >= 1;
    return true;
  });

  const mint = (layout: Layout): CollectionView => {
    const taken = new Set(data.views.map((v) => v.name));
    let name = LAYOUT_LABEL[layout];
    for (let i = 2; taken.has(name); i++) name = `${LAYOUT_LABEL[layout]} ${i}`;
    const base: CollectionView = {
      name,
      layout,
      group_by: null,
      sub_group_by: null,
      sort: [],
      filter: [],
      date_field: null,
      start_field: null,
      end_field: null,
      cover_field: null,
      columns: [],
      widths: {},
      column_width: null,
    };
    if (layout === "board") return { ...base, group_by: groupable?.key ?? null };
    if (layout === "gantt")
      return {
        ...base,
        start_field: dates[0]?.key ?? null,
        end_field: (dates[1] ?? dates[0])?.key ?? null,
      };
    if (layout === "calendar") return { ...base, date_field: dates[0]?.key ?? null };
    if (layout === "gallery") return { ...base, cover_field: url?.key ?? null };
    return base;
  };

  return (
    // A list of actions, so a Menu (the kit drew it as a popover of buttons); a row closes it.
    <Menu>
      <MenuTrigger
        aria-label="Add view"
        render={<ToggleChip className="text-muted-foreground hover:text-foreground" />}
      >
        <Plus className="size-3.5" /> View
      </MenuTrigger>
      <MenuContent align="start">
        {available.map((layout) => (
          <MenuItem key={layout} label={LAYOUT_LABEL[layout]} onClick={() => onAdd(mint(layout))} />
        ))}
      </MenuContent>
    </Menu>
  );
}
