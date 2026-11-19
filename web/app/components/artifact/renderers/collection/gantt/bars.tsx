/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useSortable } from "@dnd-kit/sortable";
import { GripVertical } from "lucide-react";
import {
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { cellString, type CollectionField, type CollectionRow } from "~/lib/collection";
import { ROW_TITLE_CLASS } from "../cells/cell";
import { dayIndex, shortDate, span } from "../core/date-math";
import { METRICS } from "../metrics";
import { MIN_BAR_PX } from "./axis";
import { clampResizeDays, previewRange, snapDays } from "./drag";

// Same leading geometry as the table's grip gutter - the rail is the primary column, so its
// rows carry the identical 32px gutter and hover grip when reorder is available.
export const GUTTER_W = METRICS.gutter;

// The vertical rhythm comes from THE lens grid (metrics.ts): gantt rows are table rows are
// list rows; the axis header is every other lens's header strip; lane headers are section
// strips. The rail is the PRIMARY column - resizable from the corner handle, persisted in
// view.widths[primary.key] (the same slot the table's primary column uses, on purpose).
export const RAIL_W = METRICS.railDefault;
export const ROW_H = METRICS.row;
export const BAR_H = METRICS.bar;
export const HEADER_H = METRICS.strip;
export const LANE_H = METRICS.subRow;

type DragMode = "move" | "start" | "end";

/** One timeline row: the sticky rail cell, the strip, and the bar - ONE flex row. The bar
 * MOVES by pointer-capture drag (both dates shift; the strip's travel guard keeps a plain
 * click opening the peek) and RESIZES by its edge handles (one date shifts; min one day).
 * Preview is LOCAL state through previewRange - the identical math commits on release, so
 * what you see is exactly what stages - and nothing stages mid-drag, so the axis cannot grow
 * under the pointer. The keyboard path for dates is the record peek's date inputs. */
export type GanttRowProps = {
  row: CollectionRow;
  start: number;
  end: number;
  primary: CollectionField;
  axisStartMs: number;
  dayPx: number;
  chartW: number;
  railW: number;
  selected: boolean;
  onSelect?: (rowId: string) => void;
  onMoveBar?: (rowId: string, deltaDays: number) => void;
  onResizeBar?: (rowId: string, edge: "start" | "end", deltaDays: number) => void;
  hasBothDates: boolean;
  tintClass?: string;
  /** Reserve the leading grip gutter (whenever reorder is wired, even if currently hidden by
   * a sort/group - stable geometry, exactly like the table's gutter). */
  gutter?: boolean;
  /** The grip element (sortable activator) - rendered inside the gutter when reorderable. */
  grip?: React.ReactNode;
  rowRef?: (el: HTMLDivElement | null) => void;
  rowStyle?: CSSProperties;
};

export function GanttRow({
  row,
  start,
  end,
  primary,
  axisStartMs,
  dayPx,
  chartW,
  railW,
  selected,
  onSelect,
  onMoveBar,
  onResizeBar,
  hasBothDates,
  tintClass,
  gutter = false,
  grip,
  rowRef,
  rowStyle,
}: GanttRowProps) {
  const label = cellString(row.cells[primary.key]) ?? row.id;
  // Click-vs-pan guard: a click that travelled is a gesture, not an open.
  const downRef = useRef<{ x: number; y: number } | null>(null);
  const [drag, setDrag] = useState<{ mode: DragMode; days: number } | null>(null);
  const dragRef = useRef<{ mode: DragMode; originX: number; active: boolean } | null>(null);

  const canMove = !!onMoveBar;
  const canResize = !!onResizeBar && hasBothDates;

  const p = drag ? previewRange(drag.mode, drag.days, start, end) : { startMs: start, endMs: end };
  const left = dayIndex(p.startMs, axisStartMs) * dayPx;
  const barW = Math.max(span(p.startMs, p.endMs) * dayPx, MIN_BAR_PX);
  const labelInside = barW > label.length * 6.5 + 16;

  const beginDrag = (mode: DragMode) => (e: ReactPointerEvent<HTMLElement>) => {
    // Resize handles stop propagation (a resize must never read as a move, and the strip's
    // click guard never arms); the bar's own pointerdown BUBBLES on purpose, so the strip
    // records the origin and suppresses the click-to-peek only when travel exceeds 4px.
    if (mode !== "move") e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    // ARMED, not active: nothing previews and nothing can stage until the pointer travels
    // 4px (the app-wide activation distance). Without this, a click's micro-jitter at fine
    // zooms (6px/day) snapped to ±1 day, staged an accidental move, and the start-sorted
    // rows visibly re-ordered from a mere touch.
    dragRef.current = { mode, originX: e.clientX, active: false };
  };
  const dragMove = (e: ReactPointerEvent<HTMLElement>) => {
    const d = dragRef.current;
    if (!d || !e.currentTarget.hasPointerCapture(e.pointerId)) return;
    if (!d.active) {
      if (Math.abs(e.clientX - d.originX) < 4) return;
      d.active = true;
      // Measure days from the ACTIVATION point - the dead-zone never counts toward day one.
      d.originX = e.clientX;
      setDrag({ mode: d.mode, days: 0 });
      document.body.style.cursor = d.mode === "move" ? "grabbing" : "ew-resize";
      document.body.style.userSelect = "none";
    }
    let days = snapDays(e.clientX - d.originX, dayPx);
    if (d.mode !== "move") days = clampResizeDays(d.mode, days, start, end);
    setDrag((s) => (s && s.days === days ? s : { mode: d.mode, days }));
  };
  const settle = (e: ReactPointerEvent<HTMLElement>) => {
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  };
  const dragEnd = (e: ReactPointerEvent<HTMLElement>) => {
    const d = dragRef.current;
    if (!d) return;
    settle(e);
    dragRef.current = null;
    const days = d.active ? (drag?.days ?? 0) : 0;
    setDrag(null);
    if (days !== 0) {
      if (d.mode === "move") onMoveBar?.(row.id, days);
      else onResizeBar?.(row.id, d.mode, days);
    }
  };
  const dragCancel = (e: ReactPointerEvent<HTMLElement>) => {
    if (!dragRef.current) return;
    settle(e);
    dragRef.current = null;
    setDrag(null); // aborted: nothing stages
  };

  return (
    <div
      ref={rowRef}
      className="group relative flex"
      style={{ height: ROW_H, ...rowStyle }}
      data-selected={selected || undefined}
    >
      <div
        className="bg-card group-data-selected:shadow-[inset_2px_0_0_0_var(--color-primary)] sticky left-0 z-20 flex shrink-0 items-stretch border-r border-b"
        style={{ width: railW }}
      >
        {gutter && (
          <span className="flex shrink-0 items-center justify-center" style={{ width: GUTTER_W }}>
            {grip}
          </span>
        )}
        <button
          type="button"
          onClick={onSelect ? () => onSelect(row.id) : undefined}
          className={`group-hover:bg-muted/40 group-data-selected:bg-muted flex h-full min-w-0 flex-1 flex-col justify-center gap-0.5 px-3 text-left transition-[background-color,box-shadow] duration-150 ${
            onSelect ? "" : "cursor-default"
          }`}
        >
          <span className={ROW_TITLE_CLASS}>{label}</span>
          <span className="text-muted-foreground text-caption-md-regular leading-tight tabular-nums">
            {shortDate(p.startMs)} – {shortDate(p.endMs)} · {span(p.startMs, p.endMs)}d
          </span>
        </button>
      </div>
      {/* A click on the lane is a pointer shortcut; the rail button and the bar are the
          keyboard's ways in, so the lane itself carries no role. */}
      <div
        role="presentation"
        className={`group-hover:bg-muted/40 group-data-selected:bg-muted relative border-b transition-[background-color] duration-150 ${
          onSelect ? "cursor-pointer" : ""
        }`}
        style={{ width: chartW }}
        onPointerDown={(e) => {
          downRef.current = { x: e.clientX, y: e.clientY };
        }}
        onClick={(e) => {
          const down = downRef.current;
          downRef.current = null;
          if (down && Math.abs(e.clientX - down.x) + Math.abs(e.clientY - down.y) > 4) return;
          onSelect?.(row.id);
        }}
      >
        {/* oxlint-disable-next-line jsx-a11y/no-static-element-interactions -- the role is
            always set (a button when it opens the record, presentation when it is only a drag
            surface), but the rule cannot read a conditional role */}
        <div
          role={onSelect ? "button" : "presentation"}
          tabIndex={onSelect ? 0 : undefined}
          aria-label={onSelect ? `Open ${label}` : undefined}
          title={`${label}: ${shortDate(p.startMs)} – ${shortDate(p.endMs)} (${span(p.startMs, p.endMs)}d)`}
          onKeyDown={
            onSelect
              ? (e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onSelect(row.id);
                  }
                }
              : undefined
          }
          onPointerDown={canMove ? beginDrag("move") : undefined}
          onPointerMove={canMove ? dragMove : undefined}
          onPointerUp={canMove ? dragEnd : undefined}
          onPointerCancel={canMove ? dragCancel : undefined}
          className={`${tintClass ?? "bg-primary/80"} absolute select-none transition-[background-color,box-shadow] duration-150 ${
            canMove ? "cursor-grab touch-none" : ""
          } ${selected ? "ring-2 ring-primary/40" : ""} ${barW < 12 ? "rounded-full" : "rounded-sm"}`}
          style={{ left, width: barW, top: (ROW_H - BAR_H) / 2, height: BAR_H }}
        >
          {labelInside && (
            <span className="text-primary-foreground block truncate px-2 text-caption-md-regular leading-[20px]">
              {label}
            </span>
          )}
          {canResize && (
            <>
              <span
                aria-hidden
                onPointerDown={beginDrag("start")}
                onPointerMove={dragMove}
                onPointerUp={dragEnd}
                onPointerCancel={dragCancel}
                onClick={(e) => e.stopPropagation()}
                className="bg-primary-foreground/40 absolute inset-y-0 left-0 w-1.5 cursor-ew-resize touch-none rounded-xs opacity-0 select-none group-hover:opacity-100"
              />
              <span
                aria-hidden
                onPointerDown={beginDrag("end")}
                onPointerMove={dragMove}
                onPointerUp={dragEnd}
                onPointerCancel={dragCancel}
                onClick={(e) => e.stopPropagation()}
                className="bg-primary-foreground/40 absolute inset-y-0 right-0 w-1.5 cursor-ew-resize touch-none rounded-xs opacity-0 select-none group-hover:opacity-100"
              />
            </>
          )}
        </div>
        {!labelInside && (
          <span
            className="text-muted-foreground pointer-events-none absolute truncate text-caption-md-regular"
            style={{ left: left + barW + 6, top: (ROW_H - 14) / 2, maxWidth: 180 }}
          >
            {label}
          </span>
        )}
      </div>
    </div>
  );
}

/** Grip-activated sortable wrapper for a gantt row: listeners live on the handle only, y-only
 * transform - the identical reorder grammar as the table's SortableRow. NOTE: a transformed
 * ancestor is the containing block for position:sticky, so rails can momentarily unstick
 * while rows shift during a drag at deep horizontal scroll - brief, drag-only, accepted. */
export function SortableGanttRow(props: GanttRowProps) {
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition } =
    useSortable({ id: props.row.id });
  return (
    <GanttRow
      {...props}
      gutter
      rowRef={setNodeRef}
      rowStyle={{
        transform: transform ? `translate3d(0, ${transform.y}px, 0)` : undefined,
        transition,
      }}
      grip={
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
      }
    />
  );
}

/** A swimlane header: the group's option pill on the sticky rail + a quiet band across the
 * chart. Same sticky/z recipe as rail cells; adds height, never width - axis math untouched. */
export function LaneHeader({
  pill,
  count,
  chartW,
  railW,
  gutter = false,
}: {
  pill: React.ReactNode;
  count: number;
  chartW: number;
  railW: number;
  gutter?: boolean;
}) {
  return (
    <div className="flex" style={{ height: LANE_H }}>
      <div
        className="bg-card sticky left-0 z-20 flex shrink-0 items-center gap-2 border-r border-b px-3"
        style={{ width: railW, paddingLeft: gutter ? GUTTER_W + 12 : undefined }}
      >
        {pill}
        <span className="text-muted-foreground text-caption-md-regular tabular-nums">{count}</span>
      </div>
      <div className="bg-muted/20 border-b" style={{ width: chartW }} />
    </div>
  );
}
