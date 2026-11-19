/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { closestCenter, DndContext, type DragEndEvent } from "@dnd-kit/core";
import { SortableContext, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { GripVertical } from "lucide-react";
import { useMemo } from "react";
import { LabeledDivider, Rows } from "~/components/kit";
import {
  fieldOf,
  groupRows,
  primaryField,
  type Collection,
  type CollectionField,
  type CollectionRow,
  type CollectionView,
} from "~/lib/collection";
import { Cell } from "../cells/cell";
import { FieldChip } from "../cells/field-chip";
import { OptionBadge } from "../cells/option-badge";
import type { MoveAnchor } from "../core/edit-state";
import { PressSurface } from "../core/press";
import { cardFields } from "../core/view-config";
import { moveAnchorFor, useCollectionSensors } from "../dnd";
import { METRICS } from "../metrics";

/** The list lens, v4 - the kit's row recipe over the projected rows: title left, the view's
 * visible cells right. `group_by` renders option-pill section headers. Rows reorder by the
 * hover grip - hidden under an active sort, and under grouping too (a cross-section drop
 * implies a group change, which is the board's gesture; recorded follow-up). */
export function ListView({
  data,
  view,
  rows: rowsProp,
  selectedRowId,
  onSelectRow,
  onMoveRow,
}: {
  data: Collection;
  view: CollectionView;
  rows?: CollectionRow[];
  selectedRowId?: string | null;
  onSelectRow?: (rowId: string) => void;
  onMoveRow?: (rowId: string, anchor: MoveAnchor) => void;
}) {
  const rows = rowsProp ?? data.rows;
  const primary = primaryField(data);
  const metas = cardFields(data, view);
  const dates = metas.filter((f) => f.type === "date").length;
  const groupField = fieldOf(data, view.group_by);
  const canReorder = !!onMoveRow && view.sort.length === 0 && !groupField;
  const sensors = useCollectionSensors();
  const rowIds = useMemo(() => rows.map((r) => r.id), [rows]);

  const sections = useMemo(() => {
    if (!groupField) return null;
    return groupRows({ ...data, rows }, groupField.key).filter((g) => g.rows.length > 0);
  }, [data, rows, groupField]);

  const onDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || !onMoveRow) return;
    const anchor = moveAnchorFor(rowIds, String(active.id), String(over.id));
    if (anchor) onMoveRow(String(active.id), anchor);
  };

  const renderRow = (row: CollectionRow) =>
    canReorder ? (
      <SortableListRow
        key={row.id}
        row={row}
        primary={primary}
        metas={metas}
        dates={dates}
        selected={selectedRowId === row.id}
        onSelectRow={onSelectRow}
      />
    ) : (
      <li
        key={row.id}
        className={`flex min-h-(--row-h) items-stretch transition-[background-color,box-shadow] duration-150 [content-visibility:auto] [contain-intrinsic-size:auto_44px] ${
          selectedRowId === row.id
            ? "bg-muted shadow-[inset_2px_0_0_0_var(--color-primary)]"
            : onSelectRow
              ? "hover:bg-muted/40"
              : ""
        } ${onSelectRow ? "cursor-pointer" : ""}`}
      >
        <PressSurface
          onPress={onSelectRow ? () => onSelectRow(row.id) : undefined}
          selected={selectedRowId === row.id}
          className="flex min-w-0 flex-1 items-center gap-3 px-3"
        >
          <RowBody row={row} primary={primary} metas={metas} dates={dates} />
        </PressSurface>
      </li>
    );

  const list = (
    <Rows as="ul" flush>
      {sections
        ? sections.flatMap((section) => [
            <li key={`§${section.key}`} className="flex h-(--subrow-h) items-center px-3">
              <LabeledDivider>
                <span className="inline-flex items-center gap-2">
                  {section.key === " none" ? (
                    <span className="font-medium">{section.label}</span>
                  ) : (
                    <OptionBadge field={groupField} value={section.label} />
                  )}
                  <span className="text-muted-foreground tabular-nums">{section.rows.length}</span>
                </span>
              </LabeledDivider>
            </li>,
            ...section.rows.map(renderRow),
          ])
        : rows.map(renderRow)}
    </Rows>
  );

  return canReorder ? (
    <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
      <SortableContext items={rowIds} strategy={verticalListSortingStrategy}>
        {list}
      </SortableContext>
    </DndContext>
  ) : (
    list
  );
}

/** Grip-activated sortable row: listeners live on the handle only, so row clicks stay
 * reserved for the peek. y-only transform - a vertical list needs no second axis. */
function SortableListRow({
  row,
  primary,
  metas,
  dates,
  selected,
  onSelectRow,
}: {
  row: CollectionRow;
  primary: CollectionField;
  metas: CollectionField[];
  dates: number;
  selected: boolean;
  onSelectRow?: (rowId: string) => void;
}) {
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition } =
    useSortable({ id: row.id });
  return (
    <li
      ref={setNodeRef}
      style={{
        transform: transform ? `translate3d(0, ${transform.y}px, 0)` : undefined,
        transition,
      }}
      className={`group flex min-h-(--row-h) cursor-pointer items-stretch transition-[background-color,box-shadow] duration-150 ${
        selected ? "bg-muted shadow-[inset_2px_0_0_0_var(--color-primary)]" : "hover:bg-muted/40"
      }`}
    >
      {/* Same 32px grip gutter as the table colgroup and the gantt rail; the body then
          resumes at the px-3 datum, so the title's x matches those lenses exactly. */}
      <span className="flex shrink-0 items-center justify-center" style={{ width: METRICS.gutter }}>
        <button
          type="button"
          ref={setActivatorNodeRef}
          {...attributes}
          {...listeners}
          aria-label="Drag to reorder"
          onClick={(e) => e.stopPropagation()}
          className="text-muted-foreground/70 hover:text-foreground flex h-6 w-8 cursor-grab touch-none items-center justify-center opacity-0 group-hover:opacity-100 focus-visible:opacity-100"
        >
          <GripVertical className="size-3.5" />
        </button>
      </span>
      <PressSurface
        onPress={onSelectRow ? () => onSelectRow(row.id) : undefined}
        selected={selected}
        className="flex min-w-0 flex-1 items-center gap-3 px-3"
      >
        <RowBody row={row} primary={primary} metas={metas} dates={dates} />
      </PressSurface>
    </li>
  );
}

function RowBody({
  row,
  primary,
  metas,
  dates,
}: {
  row: CollectionRow;
  primary: CollectionField;
  metas: CollectionField[];
  dates: number;
}) {
  return (
    <>
      <div className="min-w-0 flex-1">
        <Cell field={primary} value={row.cells[primary.key]} primary primaryLines={1} />
      </div>
      {metas.map((f) => (
        <span key={f.key} className="text-muted-foreground shrink-0 text-caption-md-regular">
          <FieldChip field={f} value={row.cells[f.key]} labeled={f.type === "date" && dates >= 2} />
        </span>
      ))}
    </>
  );
}
