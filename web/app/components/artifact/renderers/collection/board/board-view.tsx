/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import {
  closestCorners,
  DndContext,
  DragOverlay,
  type DragEndEvent,
  type DragOverEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import { SortableContext, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { useMemo, useRef, useState } from "react";
import { LabeledDivider } from "~/components/kit";
import {
  fieldOf,
  groupRows,
  primaryField,
  type Collection,
  type CollectionRow,
  type CollectionView,
} from "~/lib/collection";
import { OptionBadge } from "../cells/option-badge";
import type { MoveAnchor } from "../core/edit-state";
import { cardFields } from "../core/view-config";
import { useCollectionSensors } from "../dnd";
import { BOARD_COL_DEFAULT, BoardColumn, clampBoardW } from "./board-column";
import { CardBody, CardList } from "./board-card";

type DragCols = { activeId: string; cols: { key: string; ids: string[] }[] };

/**
 * The board lens, v4 - Trello's grammar, the revision spine's semantics, multi-container
 * sortable: a card drags BETWEEN columns (stages a cell edit) and WITHIN one (stages a
 * move_row), committed together as ONE batch on drop. During the drag the board renders from
 * a clone-on-lift local state (`dragCols`) so containers update live without touching the
 * document; the DragOverlay carries the card so the source <li> can unmount across columns.
 * A 4px activation distance keeps plain clicks as clicks (they open the record peek).
 */
export function BoardView({
  data,
  view,
  rows: rowsProp,
  onDropCard,
  onAddRow,
  selectedRowId,
  onSelectRow,
  onUpdateView,
}: {
  data: Collection;
  view: CollectionView;
  rows?: CollectionRow[];
  onDropCard?: (rowId: string, groupValue: unknown, anchor: MoveAnchor | null) => void;
  onAddRow?: (groupValue: unknown) => void;
  selectedRowId?: string | null;
  onSelectRow?: (rowId: string) => void;
  onUpdateView?: (patch: Partial<CollectionView>, opts?: { coalesce?: boolean }) => void;
}) {
  const groupKey = view.group_by ?? "";
  const field = fieldOf(data, groupKey);
  const rows = rowsProp ?? data.rows;
  const groups = useMemo(() => groupRows({ ...data, rows }, groupKey), [data, rows, groupKey]);
  const rowById = useMemo(() => new Map(rows.map((r) => [r.id, r])), [rows]);
  const sensors = useCollectionSensors();
  const draggingRef = useRef(false);
  const [dragCols, setDragCols] = useState<DragCols | null>(null);

  // The shared column width: live draft while any handle drags, ONE commit on pointerup.
  const [draftW, setDraftW] = useState<number | null>(null);
  const draftRef = useRef<number | null>(null);
  draftRef.current = draftW;
  const colW = draftW ?? view.column_width ?? BOARD_COL_DEFAULT;
  const commitWidth = () => {
    const w = draftRef.current;
    setDraftW(null);
    if (w != null) onUpdateView?.({ column_width: w });
  };

  const onDragStart = (event: DragStartEvent) => {
    draggingRef.current = true;
    setDragCols({
      activeId: String(event.active.id),
      cols: groups.map((g) => ({ key: g.key, ids: g.rows.map((r) => r.id) })),
    });
  };

  const onDragOver = (event: DragOverEvent) => {
    const { active, over } = event;
    if (!over) return;
    const activeId = String(active.id);
    const overId = String(over.id);
    setDragCols((s) => {
      if (!s) return s;
      const from = s.cols.find((c) => c.ids.includes(activeId));
      const toKey = overId.startsWith("col:")
        ? overId.slice(4)
        : s.cols.find((c) => c.ids.includes(overId))?.key;
      if (!from || !toKey || from.key === toKey) return s;
      const target = s.cols.find((c) => c.key === toKey);
      if (!target) return s;
      const overIndex = overId.startsWith("col:") ? target.ids.length : target.ids.indexOf(overId);
      return {
        ...s,
        cols: s.cols.map((c) =>
          c.key === from.key
            ? { ...c, ids: c.ids.filter((i) => i !== activeId) }
            : c.key === toKey
              ? {
                  ...c,
                  ids: [...c.ids.slice(0, overIndex), activeId, ...c.ids.slice(overIndex)],
                }
              : c,
        ),
      };
    });
  };

  const onDragEnd = (event: DragEndEvent) => {
    void event;
    const s = dragCols;
    setTimeout(() => {
      draggingRef.current = false;
    }, 0);
    setDragCols(null); // same commit as the stage below - no flicker window
    if (!s || !onDropCard) return;
    const col = s.cols.find((c) => c.ids.includes(s.activeId));
    if (!col) return;
    const groupValue =
      col.key === " none" ? null : field?.type === "checkbox" ? col.key === "Yes" : col.key;
    // Within-column position → neighbor anchor. Sub-grouped columns interleave several lists,
    // so positional intent is ambiguous there - group change only (recorded follow-up).
    let anchor: MoveAnchor | null = null;
    if (!view.sub_group_by) {
      const idx = col.ids.indexOf(s.activeId);
      const successor = col.ids[idx + 1];
      const predecessor = col.ids[idx - 1];
      anchor = successor ? { beforeId: successor } : predecessor ? { afterId: predecessor } : null;
    }
    onDropCard(s.activeId, groupValue, anchor);
  };

  const onDragCancel = () => {
    setTimeout(() => {
      draggingRef.current = false;
    }, 0);
    setDragCols(null);
  };

  // During a drag, columns render from the clone; unknown ids (a mid-drag prop change, e.g.
  // an undo landing) filter out gracefully.
  const renderCols = dragCols
    ? dragCols.cols.map((c) => ({
        key: c.key,
        label: groups.find((g) => g.key === c.key)?.label ?? c.key,
        rows: c.ids.map((id) => rowById.get(id)).filter((r): r is CollectionRow => !!r),
      }))
    : groups;

  const activeRow = dragCols ? (rowById.get(dragCols.activeId) ?? null) : null;
  const primary = primaryField(data);
  const overlayMetas = cardFields(data, view, [view.group_by, view.sub_group_by]);

  // Sized to content (w-max) inside the ViewShell's single scroller - the shell scrolls and
  // shows the scrollbar, so the last column can never clip behind an invisible edge.
  const strip = (
    <div className="flex w-max min-w-full items-start gap-3 p-3">
      {renderCols.map((group) => (
        <BoardColumn
          key={group.key}
          id={group.key}
          label={group.label}
          pill={
            group.key === " none" ? (
              <span className="truncate text-body-sm-regular font-medium">{group.label}</span>
            ) : (
              <OptionBadge field={field} value={group.label} />
            )
          }
          count={group.rows.length}
          width={colW}
          droppable={!!onDropCard}
          onAdd={
            onAddRow && group.key !== " none"
              ? () => onAddRow(field?.type === "checkbox" ? group.label === "Yes" : group.label)
              : undefined
          }
          onWidthDraft={onUpdateView ? setDraftW : undefined}
          onWidthCommit={onUpdateView ? commitWidth : undefined}
          onWidthNudge={
            onUpdateView
              ? (delta) =>
                  onUpdateView({ column_width: clampBoardW(colW + delta) }, { coalesce: true })
              : undefined
          }
          onWidthReset={onUpdateView ? () => onUpdateView({ column_width: null }) : undefined}
        >
          <SortableContext
            items={group.rows.map((r) => r.id)}
            strategy={verticalListSortingStrategy}
          >
            {view.sub_group_by ? (
              <SubGrouped
                data={data}
                view={view}
                rows={group.rows}
                subKey={view.sub_group_by}
                draggable={!!onDropCard}
                selectedRowId={selectedRowId}
                onSelectRow={onSelectRow}
                draggingRef={draggingRef}
              />
            ) : (
              <CardList
                data={data}
                view={view}
                rows={group.rows}
                exclude={view.group_by}
                draggable={!!onDropCard}
                selectedRowId={selectedRowId}
                onSelectRow={onSelectRow}
                draggingRef={draggingRef}
              />
            )}
          </SortableContext>
        </BoardColumn>
      ))}
    </div>
  );

  return onDropCard ? (
    <DndContext
      sensors={sensors}
      collisionDetection={closestCorners}
      onDragStart={onDragStart}
      onDragOver={onDragOver}
      onDragEnd={onDragEnd}
      onDragCancel={onDragCancel}
    >
      {strip}
      <DragOverlay>
        {activeRow && (
          <div
            className="bg-card cursor-grabbing rounded-md p-3.5 shadow-raised-300 ring-1 ring-inset ring-foreground/25"
            style={{ width: colW - 16 }}
          >
            <CardBody data={data} row={activeRow} primaryKey={primary.key} metas={overlayMetas} />
          </div>
        )}
      </DragOverlay>
    </DndContext>
  ) : (
    strip
  );
}

function SubGrouped({
  data,
  view,
  rows,
  subKey,
  draggable,
  selectedRowId,
  onSelectRow,
  draggingRef,
}: {
  data: Collection;
  view: CollectionView;
  rows: CollectionRow[];
  subKey: string;
  draggable: boolean;
  selectedRowId?: string | null;
  onSelectRow?: (rowId: string) => void;
  draggingRef: React.MutableRefObject<boolean>;
}) {
  const subs = groupRows({ ...data, rows }, subKey);
  return (
    <div className="space-y-2">
      {subs
        .filter((s) => s.rows.length > 0)
        .map((s) => (
          <div key={s.key}>
            <LabeledDivider>{s.label}</LabeledDivider>
            <CardList
              data={data}
              view={view}
              rows={s.rows}
              exclude={subKey}
              draggable={draggable}
              selectedRowId={selectedRowId}
              onSelectRow={onSelectRow}
              draggingRef={draggingRef}
            />
          </div>
        ))}
    </div>
  );
}
