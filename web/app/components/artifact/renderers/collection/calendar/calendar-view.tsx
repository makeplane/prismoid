/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { DndContext, useDraggable, useDroppable, type DragEndEvent } from "@dnd-kit/core";
import { CSS } from "@dnd-kit/utilities";
import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import {
  cellDateMs,
  cellString,
  primaryField,
  type Collection,
  type CollectionRow,
  type CollectionView,
} from "~/lib/collection";
import { isoDate, monthGrid, monthLabel, utcDay, type MonthCell } from "../core/date-math";
import { useCollectionSensors } from "../dnd";

const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MAX_CHIPS = 3;

type Chip = { id: string; label: string };

/** The calendar lens, v4 - a UTC month grid; rows land on their `date_field` day. When the
 * viewer can save, chips DRAG onto any day (one staged cell edit; spill cells lawfully change
 * the month). The month nav is a header strip inside the shell; the grid keeps CSS-gradient
 * hairlines and no ring of its own. "+N more" overflow stays non-draggable - the record
 * peek's date editor covers those rows. */
export function CalendarView({
  data,
  view,
  rows: rowsProp,
  selectedRowId,
  onSelectRow,
  onSetCell,
}: {
  data: Collection;
  view: CollectionView;
  rows?: CollectionRow[];
  selectedRowId?: string | null;
  onSelectRow?: (rowId: string) => void;
  onSetCell?: (rowId: string, key: string, value: unknown) => void;
}) {
  const dateKey = view.date_field ?? "";
  const primary = primaryField(data);
  const sourceRows = rowsProp ?? data.rows;
  const sensors = useCollectionSensors();
  const draggingRef = useRef(false);

  const byDay = useMemo(() => {
    const map = new Map<number, Chip[]>();
    for (const row of sourceRows) {
      const ms = cellDateMs(row.cells[dateKey]);
      if (ms == null) continue;
      const day = utcDay(ms);
      const label = cellString(row.cells[primary.key]) ?? row.id;
      map.set(day, [...(map.get(day) ?? []), { id: row.id, label }]);
    }
    return map;
  }, [sourceRows, dateKey, primary.key]);

  const latest = useMemo(() => {
    let ms: number | null = null;
    for (const row of sourceRows) {
      const value = cellDateMs(row.cells[dateKey]);
      if (value != null && (ms == null || value > ms)) ms = value;
    }
    return ms;
  }, [sourceRows, dateKey]);

  const now = new Date();
  const seed = latest != null ? new Date(latest) : now;
  const [ym, setYm] = useState<{ y: number; m: number }>({
    y: seed.getUTCFullYear(),
    m: seed.getUTCMonth(),
  });
  const cells = useMemo(() => monthGrid(ym.y, ym.m), [ym]);
  const todayDay = utcDay(Date.parse(now.toISOString().slice(0, 10) + "T00:00:00Z"));

  const step = (delta: number) =>
    setYm(({ y, m }) => {
      const next = m + delta;
      return { y: y + Math.floor(next / 12), m: ((next % 12) + 12) % 12 };
    });

  const onDragEnd = (event: DragEndEvent) => {
    setTimeout(() => {
      draggingRef.current = false;
    }, 0);
    const { active, over } = event;
    if (!over || !onSetCell) return;
    const rowId = String(active.id);
    const iso = isoDate(Number(over.id));
    const current = cellString(sourceRows.find((r) => r.id === rowId)?.cells[dateKey]);
    if (current === iso) return;
    onSetCell(rowId, dateKey, iso);
  };

  const grid = (
    <div>
      <div className="flex h-(--strip-h) items-center gap-2 border-b px-3">
        <h3 className="min-w-0 flex-1 truncate text-body-sm-regular font-medium">
          {monthLabel(ym.y, ym.m)}
        </h3>
        <IconButton
          variant="ghost"
          size="sm"
          onClick={() => step(-1)}
          aria-label="Previous month"
          icon={<Icon icon={ChevronLeft} />}
        />
        <Button
          variant="ghost"
          size="sm"
          stretch="auto"
          onClick={() => setYm({ y: now.getUTCFullYear(), m: now.getUTCMonth() })}
          label="Today"
        />
        <IconButton
          variant="ghost"
          size="sm"
          onClick={() => step(1)}
          aria-label="Next month"
          icon={<Icon icon={ChevronRight} />}
        />
      </div>

      <div className="p-3">
        <div className="text-muted-foreground grid grid-cols-7 gap-px pb-1.5 text-caption-md-regular font-medium">
          {DOW.map((d) => (
            <span key={d} className="px-2">
              {d}
            </span>
          ))}
        </div>
        <div
          className="grid grid-cols-7 overflow-hidden rounded-lg"
          style={{
            backgroundImage:
              "linear-gradient(to right, color-mix(in oklab, var(--color-foreground) 10%, transparent) 1px, transparent 1px), linear-gradient(to bottom, color-mix(in oklab, var(--color-foreground) 10%, transparent) 1px, transparent 1px)",
            backgroundSize: `${100 / 7}% ${100 / 6}%`,
          }}
        >
          {cells.map((cell) => (
            <DayCell
              key={cell.ms}
              cell={cell}
              todayDay={todayDay}
              chips={byDay.get(utcDay(cell.ms)) ?? []}
              selectedRowId={selectedRowId}
              onSelectRow={onSelectRow}
              droppable={!!onSetCell}
              draggingRef={draggingRef}
            />
          ))}
        </div>
      </div>
    </div>
  );

  return onSetCell ? (
    <DndContext
      sensors={sensors}
      onDragStart={() => {
        draggingRef.current = true;
      }}
      onDragEnd={onDragEnd}
      onDragCancel={() => {
        setTimeout(() => {
          draggingRef.current = false;
        }, 0);
      }}
    >
      {grid}
    </DndContext>
  ) : (
    grid
  );
}

function DayCell({
  cell,
  todayDay,
  chips,
  selectedRowId,
  onSelectRow,
  droppable,
  draggingRef,
}: {
  cell: MonthCell;
  todayDay: number;
  chips: Chip[];
  selectedRowId?: string | null;
  onSelectRow?: (rowId: string) => void;
  droppable: boolean;
  draggingRef: React.MutableRefObject<boolean>;
}) {
  const { isOver, setNodeRef } = useDroppable({ id: String(cell.ms), disabled: !droppable });
  return (
    <div
      ref={setNodeRef}
      aria-label={new Date(cell.ms).toISOString().slice(0, 10)}
      className={`min-h-24 p-1.5 transition-[background-color] duration-150 ${
        cell.inMonth ? "" : "text-muted-foreground/60"
      } ${isOver ? "bg-primary/10" : ""}`}
    >
      <span
        className={`text-caption-md-regular tabular-nums ${
          utcDay(cell.ms) === todayDay
            ? "bg-primary text-primary-foreground inline-grid size-5 place-items-center rounded-full font-medium"
            : ""
        }`}
      >
        {cell.day}
      </span>
      <div className="mt-1 space-y-0.5">
        {chips.slice(0, MAX_CHIPS).map((chip) =>
          droppable ? (
            <DraggableChip
              key={chip.id}
              chip={chip}
              selected={selectedRowId === chip.id}
              onSelectRow={onSelectRow}
              draggingRef={draggingRef}
            />
          ) : (
            <button
              key={chip.id}
              type="button"
              onClick={onSelectRow ? () => onSelectRow(chip.id) : undefined}
              title={chip.label}
              className={`block w-full truncate rounded-sm px-1.5 py-0.5 text-left text-caption-md-regular transition-[background-color,box-shadow] duration-150 ${
                selectedRowId === chip.id
                  ? "bg-muted ring-primary/40 ring-2 ring-inset"
                  : "bg-muted hover:bg-muted/70"
              } ${onSelectRow ? "cursor-pointer" : "cursor-default"}`}
            >
              {chip.label}
            </button>
          ),
        )}
        {chips.length > MAX_CHIPS && (
          <p
            className="text-muted-foreground px-1.5 text-caption-md-regular"
            title={chips
              .slice(MAX_CHIPS)
              .map((c) => c.label)
              .join("\n")}
          >
            +{chips.length - MAX_CHIPS} more
          </p>
        )}
      </div>
    </div>
  );
}

function DraggableChip({
  chip,
  selected,
  onSelectRow,
  draggingRef,
}: {
  chip: Chip;
  selected: boolean;
  onSelectRow?: (rowId: string) => void;
  draggingRef: React.MutableRefObject<boolean>;
}) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({
    id: chip.id,
  });
  return (
    <button
      ref={setNodeRef}
      {...attributes}
      {...listeners}
      type="button"
      onClick={() => {
        if (!draggingRef.current) onSelectRow?.(chip.id);
      }}
      title={chip.label}
      style={{ transform: CSS.Translate.toString(transform) }}
      className={`block w-full cursor-grab touch-none truncate rounded-sm px-1.5 py-0.5 text-left text-caption-md-regular transition-[background-color,box-shadow] duration-150 ${
        isDragging
          ? "bg-muted z-10 opacity-80 shadow-raised-300"
          : selected
            ? "bg-muted ring-primary/40 ring-2 ring-inset"
            : "bg-muted hover:bg-muted/70"
      }`}
    >
      {chip.label}
    </button>
  );
}
