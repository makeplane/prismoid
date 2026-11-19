/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { closestCenter, DndContext, type DragEndEvent } from "@dnd-kit/core";
import { SortableContext, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { ArrowDown, ArrowUp, GripVertical, Plus } from "lucide-react";
import { memo, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import {
  cellString,
  fieldOf,
  primaryField,
  sortRows,
  type Collection,
  type CollectionField,
  type CollectionRow,
  type CollectionView,
} from "~/lib/collection";
import { cn } from "~/lib/utils";
import { Cell, ROW_TITLE_CLASS } from "../cells/cell";
import { TYPE_ICON } from "../cells/field-icon";
import type { MoveAnchor } from "../core/edit-state";
import { moveAnchorFor, useCollectionSensors } from "../dnd";
import { METRICS } from "../metrics";
import { clampColW, COL_MAX, COL_MIN, resolveWidths } from "./widths";

type Sort = { field: string; direction: "asc" | "desc" } | null;

const GUTTER_PX = METRICS.gutter;

// A plain <table>, not Propel's: Propel's Table refuses className/style (so no
// table-fixed, no colgroup-driven px width, no resize), wraps itself in an overflow scroll frame
// (which kills the sticky THs against the shell scroller), pins rows at h-11 (off the gantt
// twin's --row-h grid) and takes no row style (the drag transform). The kit table's base classes
// live here verbatim, merged with cn as the kit did, on the semantic tokens app.css points at
// Propel's.
const TR =
  "border-b transition-colors hover:bg-muted/50 has-aria-expanded:bg-muted/50 data-[state=selected]:bg-muted";
const TH =
  "text-foreground h-10 px-2 text-left align-middle font-medium whitespace-nowrap [&:has([role=checkbox])]:pr-0";
const TD = "p-2 align-middle whitespace-nowrap [&:has([role=checkbox])]:pr-0";
const SELECTED_EDGE = "group-data-[state=selected]:shadow-[inset_2px_0_0_0_var(--color-primary)]";
const GUTTER_TD = `${SELECTED_EDGE} px-0 align-middle`;

// The non-width residue of the old COL_CLASS - widths are px in table/widths.ts now, applied
// through the <colgroup> under table-fixed. NOTE: no `content-visibility` anywhere near a <tr>;
// table rows cannot be size-contained (the smeared-table bug this file once shipped).
const COL_STYLE: Record<CollectionField["type"], string> = {
  text: "",
  number: "whitespace-nowrap text-right",
  date: "whitespace-nowrap",
  select: "",
  multi_select: "",
  checkbox: "",
  url: "",
  person: "",
};

/**
 * The base lens, v4. Layout is authoritative through <colgroup> + table-fixed; widths persist
 * per view in the DOCUMENT via update_view; a header click cycles the doc's sort when the
 * viewer can save. Rows reorder by the hover grip in the gutter - hidden under an active sort
 * (manual order is meaningless there; Notion's behavior) and mapped to the document through
 * visible-neighbor anchors, so filtered-out rows never teleport.
 */
export function TableView({
  data,
  view,
  rows: rowsProp,
  onAddRow,
  onSelectRow,
  selectedRowId,
  onUpdateView,
  onMoveRow,
}: {
  data: Collection;
  view: CollectionView;
  rows?: CollectionRow[];
  onAddRow?: () => void;
  onSelectRow?: (rowId: string) => void;
  selectedRowId?: string | null;
  onUpdateView?: (patch: Partial<CollectionView>, opts?: { coalesce?: boolean }) => void;
  onMoveRow?: (rowId: string, anchor: MoveAnchor) => void;
}) {
  const fields = useMemo(() => {
    const chosen = view.columns.length
      ? view.columns.map((k) => fieldOf(data, k)).filter((f) => f != null)
      : data.fields;
    return chosen.length ? chosen : data.fields;
  }, [data, view.columns]);

  const primaryKey = primaryField(data)?.key;
  const incoming = rowsProp ?? data.rows;
  const [readerSort, setReaderSort] = useState<Sort>(view.sort[0] ?? null);
  const rows = useMemo(() => {
    if (onUpdateView) return incoming; // saveable: the projection upstream applied view.sort
    return readerSort ? sortRows(data, incoming, [readerSort]) : incoming;
  }, [data, incoming, readerSort, onUpdateView]);

  const activeSort: Sort = onUpdateView ? (view.sort[0] ?? null) : readerSort;
  const toggle = (key: string) => {
    const next: Sort =
      activeSort?.field === key
        ? activeSort.direction === "asc"
          ? { field: key, direction: "desc" }
          : null
        : { field: key, direction: "asc" };
    if (onUpdateView) onUpdateView({ sort: next ? [next] : [] });
    else setReaderSort(next);
  };

  // Live widths exist only between pointerdown and pointerup; ONE update_view commits on up.
  const [live, setLive] = useState<Record<string, number>>({});
  const liveRef = useRef(live);
  liveRef.current = live;
  const dragRef = useRef<{ key: string; startX: number; startW: number } | null>(null);
  const resolved = resolveWidths(fields, view.widths, live);
  const withGutter = !!onMoveRow;
  const canReorder = withGutter && view.sort.length === 0;
  const totalPx =
    fields.reduce((sum, f) => sum + resolved[f.key], 0) + (withGutter ? GUTTER_PX : 0);

  const beginResize = (key: string) => (e: ReactPointerEvent<HTMLSpanElement>) => {
    e.preventDefault();
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    dragRef.current = { key, startX: e.clientX, startW: resolved[key] };
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";
  };
  const resize = (e: ReactPointerEvent<HTMLSpanElement>) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId) || !dragRef.current) return;
    const { key, startX, startW } = dragRef.current;
    setLive((l) => ({ ...l, [key]: clampColW(startW + (e.clientX - startX)) }));
  };
  const endResize = (e: ReactPointerEvent<HTMLSpanElement>) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
    e.currentTarget.releasePointerCapture(e.pointerId);
    document.body.style.userSelect = "";
    document.body.style.cursor = "";
    const drag = dragRef.current;
    dragRef.current = null;
    const w = drag ? liveRef.current[drag.key] : undefined;
    setLive({});
    if (drag && w != null) onUpdateView?.({ widths: { ...view.widths, [drag.key]: w } });
  };
  const resetWidth = (key: string) => {
    const rest = Object.fromEntries(Object.entries(view.widths).filter(([k]) => k !== key));
    onUpdateView?.({ widths: rest });
  };
  const nudge = (key: string, delta: number) =>
    onUpdateView?.(
      { widths: { ...view.widths, [key]: clampColW(resolved[key] + delta) } },
      { coalesce: true },
    );

  const rowIds = useMemo(() => rows.map((r) => r.id), [rows]);
  const sensors = useCollectionSensors();
  const onDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || !onMoveRow) return;
    const anchor = moveAnchorFor(rowIds, String(active.id), String(over.id));
    if (anchor) onMoveRow(String(active.id), anchor);
  };

  const table = (
    // No overflow element between the shell scroller and the sticky THs - one would kill them.
    <div>
      <table
        className="w-full table-fixed caption-bottom text-caption-md-regular"
        style={{ width: totalPx, minWidth: "100%" }}
      >
        <colgroup>
          {withGutter && <col style={{ width: GUTTER_PX }} />}
          {fields.map((f) => (
            <col key={f.key} style={{ width: resolved[f.key] }} />
          ))}
        </colgroup>
        <thead className="[&_tr]:border-b">
          <tr className={cn(TR, "hover:bg-transparent")}>
            {withGutter && (
              <th
                aria-hidden
                title={canReorder ? undefined : "Clear sort to reorder"}
                className={cn(TH, "bg-card sticky top-0 z-10 h-(--strip-h) border-b px-0")}
              />
            )}
            {fields.map((f, i) => {
              const Icon = TYPE_ICON[f.type];
              return (
                <th
                  key={f.key}
                  aria-sort={
                    activeSort?.field === f.key
                      ? activeSort.direction === "asc"
                        ? "ascending"
                        : "descending"
                      : undefined
                  }
                  className={cn(
                    TH,
                    "bg-card sticky top-0 z-10 h-(--strip-h) border-b px-3",
                    i > 0 && "border-l",
                    COL_STYLE[f.type],
                  )}
                >
                  <button
                    type="button"
                    onClick={() => toggle(f.key)}
                    // flex + h-full, not inline-flex: cell vertical-align centers inline boxes
                    // by baseline math, ~2px off flexbox centering - the gantt corner centers
                    // by flex, and twin strips must center by the SAME algorithm.
                    className="text-muted-foreground hover:text-foreground flex h-full max-w-full items-center gap-1.5 text-caption-md-regular font-medium transition-colors duration-150"
                  >
                    <Icon className="text-muted-foreground/70 size-3.5 shrink-0" />
                    <span className="truncate">{f.label}</span>
                    {activeSort?.field === f.key &&
                      (activeSort.direction === "asc" ? (
                        <ArrowUp className="size-3 shrink-0" />
                      ) : (
                        <ArrowDown className="size-3 shrink-0" />
                      ))}
                  </button>
                  {onUpdateView && (
                    <span
                      role="separator"
                      aria-orientation="vertical"
                      aria-label={`Resize ${f.label}`}
                      aria-valuenow={resolved[f.key]}
                      aria-valuemin={COL_MIN}
                      aria-valuemax={COL_MAX}
                      tabIndex={0}
                      onPointerDown={beginResize(f.key)}
                      onPointerMove={resize}
                      onPointerUp={endResize}
                      onPointerCancel={endResize}
                      onClick={(e) => e.stopPropagation()}
                      onDoubleClick={() => resetWidth(f.key)}
                      onKeyDown={(e) => {
                        if (e.key === "ArrowLeft") nudge(f.key, -16);
                        if (e.key === "ArrowRight") nudge(f.key, 16);
                      }}
                      // right-0, never a negative offset: sticky THs are sibling stacking
                      // contexts, so anything protruding past the boundary is painted over
                      // (and unhittable) under the NEXT header.
                      className="hover:bg-accent focus-visible:ring-ring absolute inset-y-0 right-0 z-20 w-1.5 cursor-col-resize touch-none outline-none select-none focus-visible:ring-2"
                    />
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <TableRows
          fields={fields}
          primaryKey={primaryKey}
          rows={rows}
          selectedRowId={selectedRowId}
          onSelectRow={onSelectRow}
          withGutter={withGutter}
          canReorder={canReorder}
        />
      </table>
      {/* Pinned to the scrollport (a static block never widens with the table; sticky left-0
          keeps it put during horizontal scroll): count line + the add-row affordance. */}
      <div className="bg-card text-muted-foreground sticky left-0 flex h-(--footer-h) items-center justify-between border-t px-3 text-caption-md-regular">
        {onAddRow ? (
          <button
            type="button"
            onClick={onAddRow}
            className="hover:text-foreground inline-flex items-center gap-1.5 font-medium transition-colors duration-150"
          >
            <Plus className="size-3.5" /> New row
          </button>
        ) : (
          <span />
        )}
        <span className="tabular-nums">{rows.length} rows</span>
      </div>
    </div>
  );

  return canReorder ? (
    <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
      <SortableContext items={rowIds} strategy={verticalListSortingStrategy}>
        {table}
      </SortableContext>
    </DndContext>
  ) : (
    table
  );
}

/** The body, memoized WITHOUT live widths - a resize drag re-renders only colgroup + header. */
const TableRows = memo(function TableRows({
  fields,
  primaryKey,
  rows,
  selectedRowId,
  onSelectRow,
  withGutter,
  canReorder,
}: {
  fields: CollectionField[];
  primaryKey?: string;
  rows: CollectionRow[];
  selectedRowId?: string | null;
  onSelectRow?: (rowId: string) => void;
  withGutter: boolean;
  canReorder: boolean;
}) {
  return (
    <tbody className="[&_tr:last-child]:border-0">
      {rows.map((row) =>
        canReorder ? (
          <SortableTableRow
            key={row.id}
            row={row}
            fields={fields}
            primaryKey={primaryKey}
            selected={selectedRowId === row.id}
            onSelectRow={onSelectRow}
          />
        ) : (
          <tr
            key={row.id}
            onClick={() => onSelectRow?.(row.id)}
            data-state={selectedRowId === row.id ? "selected" : undefined}
            className={cn(TR, "group h-(--row-h) cursor-pointer")}
          >
            {withGutter && <td className={cn(TD, GUTTER_TD)} />}
            <RowCells row={row} fields={fields} primaryKey={primaryKey} leading={!withGutter} />
          </tr>
        ),
      )}
    </tbody>
  );
});

/** One sortable row: the grip in the gutter is the only drag activator, so row clicks stay
 * reserved for the peek. y-only transform - a vertical list needs no second axis. */
const SortableTableRow = memo(function SortableTableRow({
  row,
  fields,
  primaryKey,
  selected,
  onSelectRow,
}: {
  row: CollectionRow;
  fields: CollectionField[];
  primaryKey?: string;
  selected: boolean;
  onSelectRow?: (rowId: string) => void;
}) {
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition } =
    useSortable({ id: row.id });
  return (
    <tr
      ref={setNodeRef}
      style={{
        transform: transform ? `translate3d(0, ${transform.y}px, 0)` : undefined,
        transition,
      }}
      onClick={() => onSelectRow?.(row.id)}
      data-state={selected ? "selected" : undefined}
      className={cn(TR, "group h-(--row-h) cursor-pointer")}
    >
      <td className={cn(TD, GUTTER_TD)}>
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
      </td>
      <RowCells row={row} fields={fields} primaryKey={primaryKey} leading={false} />
    </tr>
  );
});

/** `leading`: whether the first FIELD cell is the row's leading cell (no gutter) - the
 * selection accent edge rides the row's true leading cell, exactly once. */
function RowCells({
  row,
  fields,
  primaryKey,
  leading,
}: {
  row: CollectionRow;
  fields: CollectionField[];
  primaryKey?: string;
  leading: boolean;
}) {
  return (
    <>
      {fields.map((f, i) => (
        <td
          key={f.key}
          className={cn(
            TD,
            "px-3 py-1.5 align-middle",
            i > 0 && "border-l",
            COL_STYLE[f.type],
            leading && i === 0 && SELECTED_EDGE,
          )}
        >
          {f.type === "text" ? (
            <TextCell value={row.cells[f.key]} primary={f.key === primaryKey} />
          ) : (
            <Cell field={f} value={row.cells[f.key]} />
          )}
        </td>
      ))}
    </>
  );
}

/** Single line, always: rows sit ON the lens grid (--row-h), matching the gantt rail row for
 * row. The primary wears the rail title's exact type (text-body-sm-regular leading-tight); full text lives
 * in the peek. Only a wrapping multi_select may grow a row - height is a floor, not a cage. */
function TextCell({ value, primary }: { value: unknown; primary?: boolean }) {
  const text = cellString(value);
  if (!text) return <span className="text-muted-foreground">-</span>;
  return <div className={primary ? ROW_TITLE_CLASS : "truncate"}>{text}</div>;
}
