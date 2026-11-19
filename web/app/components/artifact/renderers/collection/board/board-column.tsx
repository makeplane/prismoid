/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useDroppable } from "@dnd-kit/core";
import { Plus } from "lucide-react";
import { useRef, type PointerEvent as ReactPointerEvent } from "react";

export const BOARD_COL_MIN = 280;
export const BOARD_COL_MAX = 480;
export const BOARD_COL_DEFAULT = 320;
export const clampBoardW = (px: number) =>
  Math.min(BOARD_COL_MAX, Math.max(BOARD_COL_MIN, Math.round(px)));

/** One droppable bucket. Every column's right edge carries the SAME width handle - boards keep
 * one shared column width (view.column_width), so dragging any edge resizes them all together;
 * the draft flows live through the parent and ONE update_view commits on pointerup. The handle
 * stops pointerdown propagation so dnd-kit never mistakes a resize for a card lift. */
export function BoardColumn({
  id,
  label,
  pill,
  count,
  width,
  droppable,
  onAdd,
  onWidthDraft,
  onWidthCommit,
  onWidthNudge,
  onWidthReset,
  children,
}: {
  id: string;
  label: string;
  pill: React.ReactNode;
  count: number;
  width: number;
  droppable: boolean;
  onAdd?: () => void;
  onWidthDraft?: (px: number) => void;
  onWidthCommit?: () => void;
  onWidthNudge?: (delta: number) => void;
  onWidthReset?: () => void;
  children: React.ReactNode;
}) {
  // Namespaced droppable id + typed data: a row id can never collide with an option named
  // like one, and the " none" sentinel leaves the id space entirely.
  const { isOver, setNodeRef } = useDroppable({
    id: `col:${id}`,
    data: { type: "column", key: id },
    disabled: !droppable,
  });
  const startRef = useRef<{ x: number; w: number } | null>(null);

  const beginResize = (e: ReactPointerEvent<HTMLSpanElement>) => {
    e.preventDefault();
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    startRef.current = { x: e.clientX, w: width };
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";
  };
  const resize = (e: ReactPointerEvent<HTMLSpanElement>) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId) || !startRef.current) return;
    onWidthDraft?.(clampBoardW(startRef.current.w + (e.clientX - startRef.current.x)));
  };
  const endResize = (e: ReactPointerEvent<HTMLSpanElement>) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
    e.currentTarget.releasePointerCapture(e.pointerId);
    startRef.current = null;
    document.body.style.userSelect = "";
    document.body.style.cursor = "";
    onWidthCommit?.();
  };

  return (
    <section
      ref={setNodeRef}
      aria-label={label}
      className={`relative shrink-0 rounded-lg p-2 transition-[background-color,box-shadow] duration-150 ${
        isOver ? "bg-muted/60 ring-2 ring-primary/40 ring-inset" : "bg-muted/40"
      }`}
      style={{ width }}
    >
      <header className="flex h-(--subrow-h) items-center gap-2">
        <h3 className="flex min-w-0 items-center">{pill}</h3>
        <span className="text-muted-foreground text-caption-md-regular tabular-nums">{count}</span>
      </header>
      <div className="min-h-16">{children}</div>
      {onAdd && (
        <button
          type="button"
          onClick={onAdd}
          className="text-muted-foreground hover:bg-muted/60 hover:text-foreground mt-1 flex w-full items-center gap-1.5 rounded-md px-3.5 py-1.5 text-left text-caption-md-regular font-medium transition-colors duration-150"
        >
          <Plus className="size-3.5" /> Add
        </button>
      )}
      {onWidthDraft && (
        <span
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize board columns"
          aria-valuenow={width}
          aria-valuemin={BOARD_COL_MIN}
          aria-valuemax={BOARD_COL_MAX}
          tabIndex={0}
          onPointerDown={beginResize}
          onPointerMove={resize}
          onPointerUp={endResize}
          onPointerCancel={endResize}
          onDoubleClick={onWidthReset}
          onKeyDown={(e) => {
            if (e.key === "ArrowLeft") onWidthNudge?.(-16);
            if (e.key === "ArrowRight") onWidthNudge?.(16);
          }}
          className="hover:bg-accent focus-visible:ring-ring absolute inset-y-2 -right-[7px] z-10 w-1.5 cursor-col-resize touch-none rounded-xs outline-none select-none focus-visible:ring-2"
        />
      )}
    </section>
  );
}
