/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { ArrowUpDown } from "lucide-react";
import { Icon } from "@makeplane/propel/components/icon";
import {
  Menu,
  MenuContent,
  MenuGroup,
  MenuItem,
  MenuLabel,
  MenuRadioGroup,
  MenuRadioItem,
  MenuSeparator,
  MenuSubmenu,
  MenuSubmenuContent,
  MenuSubmenuTrigger,
  MenuTrigger,
} from "@makeplane/propel/components/menu";
import { Button as ButtonElement } from "@makeplane/propel/elements/button";
import type { Collection, CollectionView } from "~/lib/collection";
import { TYPE_ICON } from "../cells/field-icon";

/** Sort is document state (multi-sort = the schema's list; the chips row shows precedence).
 * Setting a field's direction appends or replaces its spec; Remove drops it. */
export function SortMenu({
  data,
  view,
  onUpdateView,
}: {
  data: Collection;
  view: CollectionView;
  onUpdateView: (patch: Partial<CollectionView>) => void;
}) {
  const dirOf = (key: string) => view.sort.find((s) => s.field === key)?.direction ?? "";
  const setDir = (key: string, direction: "asc" | "desc") => {
    const rest = view.sort.filter((s) => s.field !== key);
    onUpdateView({ sort: [...rest, { field: key, direction }] });
  };
  const remove = (key: string) => onUpdateView({ sort: view.sort.filter((s) => s.field !== key) });

  return (
    <Menu>
      <MenuTrigger
        render={<ButtonElement variant="secondary" size="sm" stretch="auto" />}
        aria-label="Sort"
      >
        <ArrowUpDown className="size-3.5" /> Sort
        <span
          aria-hidden
          className={`size-1.5 rounded-full ${view.sort.length > 0 ? "bg-primary" : "bg-transparent"}`}
        />
      </MenuTrigger>
      <MenuContent align="end">
        {/* A group label needs its group (Base UI throws on a bare GroupLabel). */}
        <MenuGroup>
          <MenuLabel>Sort by</MenuLabel>
          {data.fields.map((f) => {
            const dir = dirOf(f.key);
            return (
              <MenuSubmenu key={f.key}>
                <MenuSubmenuTrigger
                  icon={<Icon icon={TYPE_ICON[f.type]} tint="tertiary" />}
                  label={f.label}
                  trailing={dir ? (dir === "asc" ? "↑" : "↓") : undefined}
                />
                <MenuSubmenuContent>
                  <MenuRadioGroup
                    value={dir}
                    onValueChange={(v) => setDir(f.key, v as "asc" | "desc")}
                  >
                    <MenuRadioItem value="asc" label="Ascending" />
                    <MenuRadioItem value="desc" label="Descending" />
                  </MenuRadioGroup>
                  {dir && (
                    <>
                      <MenuSeparator />
                      <MenuItem label="Remove" onClick={() => remove(f.key)} />
                    </>
                  )}
                </MenuSubmenuContent>
              </MenuSubmenu>
            );
          })}
        </MenuGroup>
      </MenuContent>
    </Menu>
  );
}
