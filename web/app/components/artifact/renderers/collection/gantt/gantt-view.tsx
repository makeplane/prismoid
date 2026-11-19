/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { CalendarRange } from "lucide-react";
import {
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import { EmptyState, HUE_BG } from "~/components/kit";
import {
  Menu,
  MenuContent,
  MenuRadioGroup,
  MenuRadioItem,
  MenuTrigger,
} from "@makeplane/propel/components/menu";
import {
  cellDateMs,
  cellString,
  fieldOf,
  groupRows,
  primaryField,
  type Collection,
  type CollectionRow,
  type CollectionView,
} from "~/lib/collection";
import { closestCenter, DndContext, type DragEndEvent } from "@dnd-kit/core";
import { SortableContext, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { OptionBadge, optionHueIndex } from "../cells/option-badge";
import { DAY_MS, dayIndex, isWeekend, shortDate } from "../core/date-math";
import type { MoveAnchor } from "../core/edit-state";
import { moveAnchorFor, useCollectionSensors } from "../dnd";
import { clampRail, METRICS } from "../metrics";
import { useToolbarSlot } from "../toolbar/slot";
import { ZOOMS, axisModel, type Zoom } from "./axis";
import { TYPE_ICON } from "../cells/field-icon";
import { GanttRow, GUTTER_W, HEADER_H, LaneHeader, SortableGanttRow } from "./bars";
import { useGanttViewport } from "./use-gantt-viewport";

type DatedRow = { row: CollectionRow; start: number; end: number; hasEnd: boolean };

/**
 * The gantt lens, v4: a windowed-infinite time axis inside the ViewShell's single scroller.
 * Opens fit-to-data at an auto-picked zoom, pans past the data in both directions (prepends
 * compensated so nothing visibly jumps), fills the scrollport at every zoom, and keeps the
 * rail and two-band header sticky against the one scroll element. When the viewer can save,
 * bars MOVE and RESIZE by drag (one batch per gesture); the record peek's date inputs remain
 * the keyboard path. `group_by` renders swimlanes, and bars wear the group option's hue.
 * Follow-ups on record: auto-scroll at the viewport edge mid-drag; drag-to-schedule for
 * undated rows; cross-lane bar drag (group changes stay on the board).
 */
export function GanttView({
  data,
  view,
  rows: rowsProp,
  scrollerRef,
  selectedRowId,
  onSelectRow,
  onMoveBar,
  onResizeBar,
  onMoveRow,
  onUpdateView,
  initialContainerWidth,
}: {
  data: Collection;
  view: CollectionView;
  rows?: CollectionRow[];
  scrollerRef?: RefObject<HTMLDivElement | null>;
  selectedRowId?: string | null;
  onSelectRow?: (rowId: string) => void;
  onMoveBar?: (rowId: string, deltaDays: number) => void;
  onResizeBar?: (rowId: string, edge: "start" | "end", deltaDays: number) => void;
  onMoveRow?: (rowId: string, anchor: MoveAnchor) => void;
  onUpdateView?: (patch: Partial<CollectionView>, opts?: { coalesce?: boolean }) => void;
  /** TEST SEAM: seeds the viewport width so fit math runs under static render. */
  initialContainerWidth?: number;
}) {
  const primary = primaryField(data);
  const PrimaryIcon = TYPE_ICON[primary.type];
  const startKey = view.start_field ?? "";
  const endKey = view.end_field ?? "";
  const sourceRows = rowsProp ?? data.rows;

  const { dated, undated, minMs, maxMs } = useMemo(() => {
    const dated: DatedRow[] = [];
    const undated: CollectionRow[] = [];
    for (const row of sourceRows) {
      const start = cellDateMs(row.cells[startKey]);
      const endCell = cellDateMs(row.cells[endKey]);
      const end = endCell ?? start;
      if (start == null || end == null) {
        undated.push(row);
        continue;
      }
      dated.push({
        row,
        start: Math.min(start, end),
        end: Math.max(start, end),
        hasEnd: endCell != null,
      });
    }
    // DOCUMENT order, not start-sorted: the gantt's rows are the table's rows are the list's
    // rows - one order source. Want date order? Set the view's sort rule, same as any lens.
    // (This also means dragging a bar's dates never re-sorts the rows under your cursor.)
    const minMs = dated.length ? Math.min(...dated.map((d) => d.start)) : null;
    const maxMs = dated.length ? Math.max(...dated.map((d) => d.end)) : null;
    return { dated, undated, minMs, maxMs };
  }, [sourceRows, startKey, endKey]);

  // Bars wear the group option's hue (or the first select field's when ungrouped) - the same
  // deterministic mapping as every OptionBadge, so the timeline reads status at a glance.
  const tintField = useMemo(
    () => fieldOf(data, view.group_by) ?? data.fields.find((f) => f.type === "select"),
    [data, view.group_by],
  );
  const tintFor = (row: CollectionRow): string | undefined => {
    const hue = optionHueIndex(tintField, cellString(row.cells[tintField?.key ?? ""]));
    return hue == null ? undefined : HUE_BG[hue];
  };
  const resizable = startKey !== endKey && !!fieldOf(data, endKey);

  // Swimlanes: group the DATED rows in option order; empty lanes are noise, not drop targets.
  const groupField = fieldOf(data, view.group_by);
  const lanes = useMemo(() => {
    if (!groupField) return null;
    const byId = new Map(dated.map((d) => [d.row.id, d]));
    return groupRows({ ...data, rows: dated.map((d) => d.row) }, groupField.key)
      .map((g) => ({
        key: g.key,
        label: g.label,
        items: g.rows.map((r) => byId.get(r.id)).filter((d): d is DatedRow => !!d),
      }))
      .filter((g) => g.items.length > 0);
  }, [dated, data, groupField]);

  // Every hook above the empty-state return (rules of hooks); the math is safe on zero rows.
  const localRef = useRef<HTMLDivElement | null>(null);
  const { axis, zoom, dayPx, setZoom, scrollToToday, todayMs } = useGanttViewport({
    scrollerRef: scrollerRef ?? localRef,
    dataMinMs: minMs,
    dataMaxMs: maxMs,
    initialContainerWidth,
  });
  const model = useMemo(() => axisModel(zoom, axis.startMs, axis.endMs), [zoom, axis]);
  const totalDays = dayIndex(axis.endMs, axis.startMs) + 1;
  const chartW = totalDays * dayPx;
  const weekendBands = useMemo(() => {
    if (!model.weekends) return [];
    const bands: number[] = [];
    for (let d = 0; d < totalDays; d++) {
      if (isWeekend(axis.startMs + d * DAY_MS) && !isWeekend(axis.startMs + (d - 1) * DAY_MS)) {
        bands.push(d);
      }
    }
    return bands;
  }, [model.weekends, axis.startMs, totalDays]);
  const todayInAxis = todayMs >= axis.startMs && todayMs <= axis.endMs;
  const todayX = dayIndex(todayMs, axis.startMs) * dayPx;
  const slot = useToolbarSlot();

  // Reorder: identical grammar and conditions as the table - grips in the rail's gutter,
  // hidden under an active sort rule or grouping; anchors map through visible neighbors.
  const sensors = useCollectionSensors();
  const withGutter = !!onMoveRow;
  const canReorder = withGutter && view.sort.length === 0 && !groupField;
  const datedIds = useMemo(() => dated.map((d) => d.row.id), [dated]);
  const onRowDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || !onMoveRow) return;
    const anchor = moveAnchorFor(datedIds, String(active.id), String(over.id));
    if (anchor) onMoveRow(String(active.id), anchor);
  };

  // The rail IS the primary column: same slider grammar as the table's header handles, same
  // persistence slot (view.widths[primary.key]) - resizing it here resizes the table's
  // primary column too, one variable across lenses. Live draft during the drag, ONE
  // update_view on release.
  const [railDraft, setRailDraft] = useState<number | null>(null);
  const railDraftRef = useRef<number | null>(null);
  railDraftRef.current = railDraft;
  const railDragRef = useRef<{ startX: number; startW: number } | null>(null);
  const railW = railDraft ?? clampRail(view.widths[primary.key] ?? METRICS.railDefault);
  const beginRail = (e: ReactPointerEvent<HTMLSpanElement>) => {
    e.preventDefault();
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    railDragRef.current = { startX: e.clientX, startW: railW };
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";
  };
  const moveRail = (e: ReactPointerEvent<HTMLSpanElement>) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId) || !railDragRef.current) return;
    const { startX, startW } = railDragRef.current;
    setRailDraft(clampRail(startW + (e.clientX - startX)));
  };
  const endRail = (e: ReactPointerEvent<HTMLSpanElement>) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
    e.currentTarget.releasePointerCapture(e.pointerId);
    document.body.style.userSelect = "";
    document.body.style.cursor = "";
    const w = railDraftRef.current;
    railDragRef.current = null;
    setRailDraft(null);
    if (w != null) onUpdateView?.({ widths: { ...view.widths, [primary.key]: w } });
  };
  const resetRail = () => {
    const rest = Object.fromEntries(Object.entries(view.widths).filter(([k]) => k !== primary.key));
    onUpdateView?.({ widths: rest });
  };

  if (dated.length === 0) {
    return (
      <div className="p-6">
        <EmptyState
          compact
          title="Nothing scheduled"
          description="No rows carry both dates this timeline needs."
        />
      </div>
    );
  }

  // One compact dropdown, not four chips - the toolbar carries Filter · Sort · Fields too,
  // and five zoom controls were crowding every other affordance off the row.
  const controls = (
    <>
      <Menu>
        <MenuTrigger
          render={
            <Button
              variant="secondary"
              size="sm"
              stretch="auto"
              icon={<Icon icon={CalendarRange} />}
              label={ZOOMS[zoom].label}
            />
          }
          aria-label="Zoom"
        />
        <MenuContent align="end">
          <MenuRadioGroup value={zoom} onValueChange={(v) => setZoom(v as Zoom)}>
            {(Object.keys(ZOOMS) as Zoom[]).map((z) => (
              <MenuRadioItem key={z} value={z} label={ZOOMS[z].label} />
            ))}
          </MenuRadioGroup>
        </MenuContent>
      </Menu>
      <Button variant="secondary" size="sm" stretch="auto" label="Today" onClick={scrollToToday} />
    </>
  );

  const content = (
    <>
      {slot ? (
        createPortal(controls, slot)
      ) : (
        // The permanent fallback when no toolbar is mounted (tests, bare renders): a strip
        // pinned to the scrollport's left edge, scrolling away vertically like content.
        <div className="bg-card sticky left-0 flex items-center justify-end gap-1.5 border-b px-2 py-1.5">
          {controls}
        </div>
      )}

      <div style={{ width: railW + chartW }}>
        {/* HEADER - z-30 over everything; the corner is sticky BOTH ways (left within top). */}
        {/* border-b rides the CHILDREN (not this wrapper) so the corner's box model matches a
            table <th> exactly - same 44px border-box, same centering, no 1px drift. */}
        <div className="bg-card sticky top-0 z-30 flex" style={{ height: HEADER_H }}>
          {/* The corner IS the primary column's header - the same [type glyph + label] recipe,
              gutter inset, font and padding as the table's first header cell. The schedule
              summary lives in the footer strip, mirroring the table's footer. */}
          <div
            className="bg-card sticky left-0 z-10 flex shrink-0 items-center border-r border-b text-caption-md-regular font-medium"
            style={{ width: railW }}
          >
            {withGutter && <span className="shrink-0" style={{ width: GUTTER_W }} />}
            <span className="text-muted-foreground flex min-w-0 items-center gap-1.5 px-3">
              <PrimaryIcon className="text-muted-foreground/70 size-3.5 shrink-0" />
              <span className="truncate">{primary.label}</span>
            </span>
            {onUpdateView && (
              <span
                role="separator"
                aria-orientation="vertical"
                aria-label={`Resize ${primary.label}`}
                aria-valuenow={railW}
                aria-valuemin={METRICS.railMin}
                aria-valuemax={METRICS.railMax}
                tabIndex={0}
                onPointerDown={beginRail}
                onPointerMove={moveRail}
                onPointerUp={endRail}
                onPointerCancel={endRail}
                onDoubleClick={resetRail}
                onKeyDown={(e) => {
                  if (e.key === "ArrowLeft")
                    onUpdateView(
                      { widths: { ...view.widths, [primary.key]: clampRail(railW - 16) } },
                      { coalesce: true },
                    );
                  if (e.key === "ArrowRight")
                    onUpdateView(
                      { widths: { ...view.widths, [primary.key]: clampRail(railW + 16) } },
                      { coalesce: true },
                    );
                }}
                className="hover:bg-accent focus-visible:ring-ring absolute inset-y-0 right-0 z-20 w-1.5 cursor-col-resize touch-none outline-none select-none focus-visible:ring-2"
              />
            )}
          </div>
          <div className="relative border-b" style={{ width: chartW }}>
            {model.top.map((m) => (
              <span
                key={`${m.label}-${m.startDay}`}
                className="text-muted-foreground absolute top-1 truncate text-caption-md-regular font-medium"
                style={{ left: m.startDay * dayPx + 6, maxWidth: Math.max(m.days * dayPx - 8, 24) }}
              >
                {m.label}
              </span>
            ))}
            {model.labels.map((tick) => {
              const idx = dayIndex(tick.ms, axis.startMs);
              return zoom === "week" ? (
                <span
                  key={tick.ms}
                  className="text-muted-foreground/80 absolute bottom-1 text-center text-caption-md-regular tabular-nums"
                  style={{ left: idx * dayPx, width: dayPx }}
                >
                  {tick.label}
                </span>
              ) : (
                <span
                  key={tick.ms}
                  className="text-muted-foreground/80 absolute bottom-1 text-caption-md-regular whitespace-nowrap tabular-nums"
                  style={{ left: idx * dayPx + 4 }}
                >
                  {tick.label}
                </span>
              );
            })}
            {todayInAxis && (
              // Top band, not bottom: the tick labels live in the bottom band and the tag
              // would collide with whichever month/day it lands on.
              <span
                className="text-destructive absolute top-1 text-caption-md-regular font-medium"
                style={{ left: todayX + 3 }}
              >
                Today
              </span>
            )}
          </div>
        </div>

        {/* BODY - backdrop (z-0) under rows; rails z-20; the today line z-10 between. */}
        <div className="relative">
          <div
            aria-hidden
            className="pointer-events-none absolute inset-y-0 z-0"
            style={{ left: railW, width: chartW }}
          >
            {weekendBands.map((d) => (
              <div
                key={d}
                className="bg-foreground/5 absolute inset-y-0"
                style={{ left: d * dayPx, width: 2 * dayPx }}
              />
            ))}
            {model.gridlines.map((d) => (
              <div
                key={`g${d}`}
                className="bg-foreground/10 absolute inset-y-0 w-px"
                style={{ left: d * dayPx }}
              />
            ))}
          </div>
          {lanes
            ? lanes.map((lane) => (
                <div key={lane.key}>
                  <LaneHeader
                    pill={
                      lane.key === " none" ? (
                        <span className="truncate text-body-sm-regular font-medium">
                          {lane.label}
                        </span>
                      ) : (
                        <OptionBadge field={groupField} value={lane.label} />
                      )
                    }
                    count={lane.items.length}
                    chartW={chartW}
                    railW={railW}
                    gutter={withGutter}
                  />
                  {lane.items.map((d) => (
                    <GanttRow
                      key={d.row.id}
                      row={d.row}
                      start={d.start}
                      end={d.end}
                      primary={primary}
                      axisStartMs={axis.startMs}
                      dayPx={dayPx}
                      chartW={chartW}
                      railW={railW}
                      selected={selectedRowId === d.row.id}
                      onSelect={onSelectRow}
                      onMoveBar={onMoveBar}
                      onResizeBar={onResizeBar}
                      hasBothDates={d.hasEnd && resizable}
                      tintClass={tintFor(d.row)}
                      gutter={withGutter}
                    />
                  ))}
                </div>
              ))
            : dated.map((d) => {
                const Row = canReorder ? SortableGanttRow : GanttRow;
                return (
                  <Row
                    key={d.row.id}
                    row={d.row}
                    start={d.start}
                    end={d.end}
                    primary={primary}
                    axisStartMs={axis.startMs}
                    dayPx={dayPx}
                    chartW={chartW}
                    railW={railW}
                    selected={selectedRowId === d.row.id}
                    onSelect={onSelectRow}
                    onMoveBar={onMoveBar}
                    onResizeBar={onResizeBar}
                    hasBothDates={d.hasEnd && resizable}
                    tintClass={tintFor(d.row)}
                    gutter={withGutter}
                  />
                );
              })}
          {todayInAxis && (
            <div
              aria-label="today"
              className="bg-destructive/70 absolute inset-y-0 z-10 w-px"
              style={{ left: railW + todayX }}
            />
          )}
        </div>
      </div>

      {/* Footer strip - the table's footer grammar verbatim: affordance/absence left, count
          right, one h-(--footer-h) line pinned to the scrollport. */}
      <div className="bg-card text-muted-foreground sticky left-0 flex h-(--footer-h) items-center justify-between gap-3 border-t px-3 text-caption-md-regular">
        {undated.length > 0 ? (
          <span
            className="min-w-0 truncate"
            title={undated.map((row) => cellString(row.cells[primary.key]) ?? row.id).join("\n")}
          >
            No dates ({undated.length}) ·{" "}
            {undated.map((row) => cellString(row.cells[primary.key]) ?? row.id).join(" · ")}
          </span>
        ) : (
          <span />
        )}
        <span className="tabular-nums whitespace-nowrap">
          {dated.length} scheduled · {minMs != null && shortDate(minMs)} –{" "}
          {maxMs != null && shortDate(maxMs)}
        </span>
      </div>
    </>
  );

  return canReorder ? (
    <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onRowDragEnd}>
      <SortableContext items={datedIds} strategy={verticalListSortingStrategy}>
        {content}
      </SortableContext>
    </DndContext>
  ) : (
    content
  );
}
