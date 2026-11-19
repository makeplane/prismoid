/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { X } from "lucide-react";
import { useState } from "react";
import { MetaChip, ToggleChip } from "~/components/kit";
import type { Collection, CollectionView } from "~/lib/collection";
import { AddView } from "./add-view";
import { FieldsMenu } from "./fields-menu";
import { FilterChips } from "./filter-chips";
import { FilterMenu } from "./filter-menu";
import { GroupMenu } from "./group-menu";
import { SortMenu } from "./sort-menu";

/** One bar for every lens, with a FIXED two-row skeleton so switching views never bounces the
 * shell vertically (the old single wrapping row grew a second line exactly when a lens added
 * controls - the gantt's Zoom/Today - and the whole artifact jumped).
 *
 * Row 1 - which views exist, where you are, the quiet meta.
 * Row 2 - the control row, SAME height on every lens: the per-lens slot on the left (gantt
 * zoom + Today portal here; empty elsewhere), the document engine (Filter · Sort · Fields ·
 * Group) on the right. Contents change per lens; the row never appears or disappears.
 * Row 3 - active rule pills, only when rules exist.
 *
 * Naming: double-click a chip to rename it inline (savers only); a freshly added view opens
 * straight into the input. Commit on Enter/blur, revert on Esc. Names must stay UNIQUE and
 * non-empty - update_view/remove_view target views BY NAME, so a collision would make later
 * edits ambiguous; a colliding or empty draft reverts silently. */
export function ViewToolbar({
  data,
  view,
  selected,
  canEdit,
  projectedCount,
  stagedCount,
  onSelect,
  onRemoveView,
  onAddView,
  onUpdateView,
  slotRef,
}: {
  data: Collection;
  view: CollectionView;
  selected: number;
  canEdit: boolean;
  projectedCount: number;
  stagedCount: number;
  onSelect: (index: number) => void;
  onRemoveView: (view: CollectionView, index: number) => void;
  onAddView: (view: CollectionView) => void;
  onUpdateView: (patch: Partial<CollectionView>) => void;
  slotRef: (el: HTMLElement | null) => void;
}) {
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState("");
  const beginRename = (name: string) => {
    setDraft(name);
    setRenaming(true);
  };
  const commitRename = () => {
    setRenaming(false);
    const name = draft.trim();
    if (!name || name === view.name) return;
    if (data.views.some((v, i) => i !== selected && v.name === name)) return;
    onUpdateView({ name });
  };
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {data.views.map((v, i) =>
          canEdit && renaming && i === selected ? (
            // The active chip's exact skin around a content-sized input - renaming happens
            // IN PLACE, nothing opens, nothing moves.
            <span
              key={v.name}
              className="border-primary bg-primary text-primary-foreground inline-flex items-center rounded-pill border px-3 py-1 text-caption-md-regular font-medium"
            >
              <input
                autoFocus
                value={draft}
                size={Math.max(4, draft.length)}
                aria-label="View name"
                onFocus={(e) => e.currentTarget.select()}
                onChange={(e) => setDraft(e.target.value)}
                onBlur={commitRename}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commitRename();
                  if (e.key === "Escape") setRenaming(false);
                }}
                className="bg-transparent outline-none"
              />
            </span>
          ) : (
            <span key={v.name} className="group/chip relative inline-flex">
              <ToggleChip
                active={i === selected}
                title={canEdit ? "Double-click to rename" : undefined}
                onClick={() => {
                  setRenaming(false);
                  onSelect(i);
                }}
                onDoubleClick={
                  canEdit
                    ? () => {
                        onSelect(i);
                        beginRename(v.name);
                      }
                    : undefined
                }
              >
                {v.name}
                {canEdit && data.views.length > 1 && i === selected && (
                  <X
                    role="button"
                    aria-label={`Remove view ${v.name}`}
                    className="hover:text-destructive -mr-0.5 size-3"
                    onClick={(e) => {
                      e.stopPropagation();
                      onRemoveView(v, i);
                    }}
                  />
                )}
              </ToggleChip>
            </span>
          ),
        )}
        {canEdit && (
          <AddView
            data={data}
            onAdd={(v) => {
              onAddView(v);
              beginRename(v.name); // the fresh chip lands selected - name it right away
            }}
          />
        )}
        <span className="text-muted-foreground ml-auto flex items-center gap-3 pl-1.5">
          <MetaChip>{projectedCount} rows</MetaChip>
          <MetaChip>{data.fields.length} fields</MetaChip>
          {stagedCount > 0 && <MetaChip>{stagedCount} staged</MetaChip>}
        </span>
      </div>

      {/* min-h pins the row's height even when a lens contributes nothing, so view-switches
          hold still vertically. ANCHORING LAW: one right-aligned cluster; the shared trio
          (Filter · Sort · Fields) is pinned OUTERMOST in fixed order so it never moves a
          pixel between lenses; lens-specific controls (Group; the gantt's Zoom/Today slot)
          prepend on the cluster's inside edge - they join the family, they never displace it. */}
      <div className="flex min-h-7 flex-wrap items-center justify-end gap-1.5">
        <span ref={slotRef} className="flex items-center gap-1.5 empty:hidden" />
        {canEdit && (
          <>
            {["board", "gantt", "list"].includes(view.layout) && (
              <GroupMenu data={data} view={view} onUpdateView={onUpdateView} />
            )}
            <FilterMenu data={data} view={view} onUpdateView={onUpdateView} />
            <SortMenu data={data} view={view} onUpdateView={onUpdateView} />
            <FieldsMenu data={data} view={view} onUpdateView={onUpdateView} />
          </>
        )}
      </div>

      {canEdit && <FilterChips data={data} view={view} onUpdateView={onUpdateView} />}
    </div>
  );
}
