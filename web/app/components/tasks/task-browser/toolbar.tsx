/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The browser's toolbar pieces, Linear's grammar: the Filter menu + active chips live on
 *  the row's LEFT (what you're looking at); the one Display menu lives on the RIGHT (how
 *  it's shown - layout + group-by). Icon-only triggers; ink says "not default". */

import { Columns3, LayoutList, ListFilter, SlidersHorizontal, X } from "lucide-react";
import { StatusMark } from "~/components/kit";
import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import {
  Menu,
  MenuContent,
  MenuItem,
  MenuLabel,
  MenuRadioGroup,
  MenuRadioItem,
  MenuSeparator,
  MenuTrigger,
} from "@makeplane/propel/components/menu";
import {
  MODES,
  TASK_GROUPS,
  type GroupBy,
  type ProjectRow,
  type TaskFilters,
  type View,
} from "./data";

/** ONE Display menu (the sliders): how the list is shown - layout, then group-by. This
 *  replaced the labeled "Group: Status" button and the List|Board rack.
 *  Capability law: the menu offers only what the current layout can
 *  honor - an impossible option is ABSENT, never disabled with an excuse. Date/Month
 *  group boards too (columns are buckets, whatever the dimension); only "None" vanishes
 *  on a board, because a board is grouped by definition. */
export function DisplayMenu({
  group,
  view,
  projectDimension,
  setParam,
  setView,
}: {
  group: GroupBy;
  view: View;
  projectDimension: boolean;
  setParam: (key: string, value: string | null) => void;
  setView: (v: View) => void;
}) {
  const nonDefault = view !== "list" || group !== "status";
  return (
    <Menu>
      <MenuTrigger
        render={
          <IconButton
            variant={nonDefault ? "tertiary" : "ghost"}
            size="sm"
            icon={<Icon icon={SlidersHorizontal} />}
            aria-label="Display options"
          />
        }
      />
      <MenuContent align="end">
        {/* Base UI law: a menu label lives INSIDE its group - outside it there is no
            MenuGroupContext and the menu crashes on open. */}
        <MenuRadioGroup value={view} onValueChange={(v) => setView(v as View)}>
          <MenuLabel>Layout</MenuLabel>
          <MenuRadioItem value="list" label="List" icon={<Icon icon={LayoutList} />} />
          <MenuRadioItem value="board" label="Board" icon={<Icon icon={Columns3} />} />
        </MenuRadioGroup>
        <MenuSeparator />
        <MenuRadioGroup
          value={group}
          onValueChange={(v) => setParam("group", v === "status" ? null : (v as string))}
        >
          <MenuLabel>Group by</MenuLabel>
          <MenuRadioItem value="status" label="Status" />
          {projectDimension && <MenuRadioItem value="project" label="Project" />}
          <MenuRadioItem value="mode" label="Mode" />
          <MenuRadioItem value="date" label="Date" />
          <MenuRadioItem value="month" label="Month" />
          {view !== "board" && <MenuRadioItem value="none" label="None" />}
        </MenuRadioGroup>
      </MenuContent>
    </Menu>
  );
}

export function FilterMenu({
  filters,
  projects,
  setParam,
  onClear,
}: {
  filters: TaskFilters;
  projects: ProjectRow[];
  setParam: (key: string, value: string | null) => void;
  /** Clears every filter in ONE batched update - four sequential setParam calls each read
   *  the original URL and the last write wins (the clear-only-cleared-one bug). */
  onClear: () => void;
}) {
  const active = [
    filters.statusKey,
    filters.mode,
    filters.project,
    filters.archived || null,
  ].filter(Boolean).length;
  return (
    <Menu>
      {/* icon-only; with filters applied the trigger widens for the count and inks up -
          the chips beside it state WHICH filters, so the word "Filter" earns nothing */}
      <MenuTrigger
        render={
          active > 0 ? (
            <Button
              variant="tertiary"
              size="sm"
              stretch="auto"
              icon={<Icon icon={ListFilter} />}
              label={String(active)}
              aria-label="Filter"
            />
          ) : (
            <IconButton
              variant="ghost"
              size="sm"
              icon={<Icon icon={ListFilter} />}
              aria-label="Filter"
            />
          )
        }
      />
      <MenuContent align="end">
        <MenuRadioGroup
          value={filters.statusKey ?? "any"}
          onValueChange={(v) => setParam("status", v === "any" ? null : (v as string))}
        >
          <MenuLabel>Status</MenuLabel>
          <MenuRadioItem value="any" label="Any" />
          {TASK_GROUPS.map((g) => (
            // the group wears its mark - states read as glyphs, not colored dots
            <MenuRadioItem
              key={g.key}
              value={g.key}
              label={g.label}
              icon={<StatusMark status={g.dot} decorative />}
            />
          ))}
        </MenuRadioGroup>
        <MenuSeparator />
        <MenuRadioGroup
          value={filters.mode ?? "any"}
          onValueChange={(v) => setParam("mode", v === "any" ? null : (v as string))}
        >
          <MenuLabel>Mode</MenuLabel>
          <MenuRadioItem value="any" label="Any" />
          {MODES.map((m) => (
            <MenuRadioItem key={m} value={m} label={m[0].toUpperCase() + m.slice(1)} />
          ))}
        </MenuRadioGroup>
        {projects.length > 0 && (
          <>
            <MenuSeparator />
            <div className="scrollbar-thin max-h-56 overflow-y-auto">
              <MenuRadioGroup
                value={filters.project ?? "any"}
                onValueChange={(v) => setParam("project", v === "any" ? null : (v as string))}
              >
                <MenuLabel>Project</MenuLabel>
                <MenuRadioItem value="any" label="Any" />
                {projects.map((s) => (
                  <MenuRadioItem key={s.id} value={s.id} label={s.name} />
                ))}
              </MenuRadioGroup>
            </div>
          </>
        )}
        <MenuSeparator />
        {/* the archive drawer is a VIEW: default lists are the active world, "Archived"
            shows only the drawer - never both (the server enforces the same split) */}
        <MenuRadioGroup
          value={filters.archived ? "archived" : "active"}
          onValueChange={(v) => setParam("archived", v === "archived" ? "1" : null)}
        >
          <MenuLabel>View</MenuLabel>
          <MenuRadioItem value="active" label="Active" />
          <MenuRadioItem value="archived" label="Archived" />
        </MenuRadioGroup>
        {active > 0 && (
          <>
            <MenuSeparator />
            <MenuItem label="Clear filters" onClick={onClear} />
          </>
        )}
      </MenuContent>
    </Menu>
  );
}

/** Active filters, stated in plain view - a filtered list that doesn't say so is a trap. */
export function FilterChips({
  filters,
  projectName,
  setParam,
}: {
  filters: TaskFilters;
  projectName: Map<string, string>;
  setParam: (key: string, value: string | null) => void;
}) {
  const chips = [
    filters.archived && { key: "archived", label: "Archived" },
    filters.statusKey && {
      key: "status",
      label: `Status: ${TASK_GROUPS.find((g) => g.key === filters.statusKey)?.label}`,
    },
    filters.mode && { key: "mode", label: `Mode: ${filters.mode}` },
    filters.project && {
      key: "project",
      label: `Project: ${projectName.get(filters.project) ?? "…"}`,
    },
  ].filter(Boolean) as { key: string; label: string }[];
  if (chips.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {chips.map((c) => (
        <button
          key={c.key}
          onClick={() => setParam(c.key, null)}
          className="ring-foreground/10 text-muted-foreground hover:text-foreground flex items-center gap-1 rounded-pill py-0.5 pr-1.5 pl-2.5 text-caption-md-regular ring-1 ring-inset transition-colors"
          aria-label={`Remove filter ${c.label}`}
        >
          {c.label}
          <X className="size-3" />
        </button>
      ))}
    </div>
  );
}
