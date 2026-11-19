/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Icon } from "@makeplane/propel/components/icon";
import { Button as ButtonElement } from "@makeplane/propel/elements/button";
import {
  Menu,
  MenuCheckboxItem,
  MenuContent,
  MenuItem,
  MenuSeparator,
  MenuTrigger,
} from "@makeplane/propel/components/menu";
import { Columns3 } from "lucide-react";
import type { Collection, CollectionView } from "~/lib/collection";
import { TYPE_ICON } from "../cells/field-icon";
import { visibleFields } from "../core/view-config";

/** Which fields this view shows - table columns, card chips, all of it: `view.columns` is the
 * one list. Unchecking while columns==[] materializes the full list minus that field, so the
 * document stays explicit about what it means. */
export function FieldsMenu({
  data,
  view,
  onUpdateView,
}: {
  data: Collection;
  view: CollectionView;
  onUpdateView: (patch: Partial<CollectionView>) => void;
}) {
  const visible = new Set(visibleFields(data, view).map((f) => f.key));

  const toggle = (key: string, checked: boolean) => {
    if (checked) {
      onUpdateView({ columns: view.columns.length ? [...view.columns, key] : [] });
    } else {
      const all = view.columns.length ? view.columns : data.fields.map((f) => f.key);
      onUpdateView({ columns: all.filter((k) => k !== key) });
    }
  };

  return (
    <Menu>
      <MenuTrigger
        render={<ButtonElement variant="secondary" size="sm" stretch="auto" />}
        aria-label="Fields"
      >
        <Icon icon={Columns3} /> Fields
        {/* A fixed-size state dot, never a count badge: badge width varies per view and
            right-anchored neighbors would slide - landmarks keep constant width. */}
        <span
          aria-hidden
          className={`size-1.5 rounded-full ${view.columns.length > 0 ? "bg-primary" : "bg-transparent"}`}
        />
      </MenuTrigger>
      <MenuContent align="end">
        {data.fields.map((f) => {
          const checked = visible.has(f.key);
          return (
            <MenuCheckboxItem
              key={f.key}
              checked={checked}
              disabled={checked && visible.size === 1}
              onCheckedChange={(next) => toggle(f.key, !!next)}
              icon={<Icon icon={TYPE_ICON[f.type]} tint="tertiary" />}
              label={f.label}
            />
          );
        })}
        {view.columns.length > 0 && (
          <>
            <MenuSeparator />
            <MenuItem label="Show all" onClick={() => onUpdateView({ columns: [] })} />
          </>
        )}
      </MenuContent>
    </Menu>
  );
}
