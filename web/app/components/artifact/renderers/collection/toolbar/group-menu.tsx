/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { ButtonLabel } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { Button as ButtonElement } from "@makeplane/propel/elements/button";
import { Layers } from "lucide-react";
import {
  Menu,
  MenuContent,
  MenuLabel,
  MenuRadioGroup,
  MenuRadioItem,
  MenuSeparator,
  MenuTrigger,
} from "@makeplane/propel/components/menu";
import type { Collection, CollectionView } from "~/lib/collection";

// Mirrors the backend's GROUPABLE: a column set must be enumerable and small.
const GROUPABLE = new Set(["select", "person", "checkbox"]);

/** Which field makes the columns (board) or the swimlanes/sections (gantt, list). Grouping is
 * STRUCTURAL on a board - no "None", the schema requires group_by - and an optional lens
 * elsewhere, so gantt/list offer None. Sub-grouping stays board-only. Table grouping is a
 * recorded follow-up. */
export function GroupMenu({
  data,
  view,
  onUpdateView,
}: {
  data: Collection;
  view: CollectionView;
  onUpdateView: (patch: Partial<CollectionView>) => void;
}) {
  const groupable = data.fields.filter((f) => GROUPABLE.has(f.type));
  const optional = view.layout !== "board";

  return (
    <Menu>
      {/* ButtonElement: the active-grouping dot rides beside the label, past Button's one icon slot. */}
      <MenuTrigger
        render={<ButtonElement variant="secondary" size="sm" stretch="auto" />}
        aria-label="Group"
      >
        <Icon icon={Layers} />
        <ButtonLabel>Group</ButtonLabel>
        <span
          aria-hidden
          className={`size-1.5 rounded-full ${!!view.group_by && optional ? "bg-primary" : "bg-transparent"}`}
        />
      </MenuTrigger>
      <MenuContent align="end">
        <MenuRadioGroup
          value={view.group_by ?? ""}
          onValueChange={(v) => onUpdateView({ group_by: v === "" ? null : String(v) })}
        >
          <MenuLabel>Group by</MenuLabel>
          {optional && <MenuRadioItem value="" label="None" />}
          {groupable.map((f) => (
            <MenuRadioItem key={f.key} value={f.key} label={f.label} />
          ))}
        </MenuRadioGroup>
        {view.layout === "board" && (
          <>
            <MenuSeparator />
            <MenuRadioGroup
              value={view.sub_group_by ?? "none"}
              onValueChange={(v) => onUpdateView({ sub_group_by: v === "none" ? null : String(v) })}
            >
              <MenuLabel>Sub-group</MenuLabel>
              <MenuRadioItem value="none" label="None" />
              {groupable
                .filter((f) => f.key !== view.group_by)
                .map((f) => (
                  <MenuRadioItem key={f.key} value={f.key} label={f.label} />
                ))}
            </MenuRadioGroup>
          </>
        )}
      </MenuContent>
    </Menu>
  );
}
