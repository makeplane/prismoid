/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { ChevronDown, ChevronUp, Trash2, X } from "lucide-react";
import { useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { MetaChip } from "~/components/kit";
import { cellString, primaryField, type Collection, type CollectionRow } from "~/lib/collection";
import { STORAGE } from "~/lib/storage";
import { Cell } from "../cells/cell";
import { CellEditor } from "../cells/cell-editor";
import { TYPE_ICON } from "../cells/field-icon";

const PEEK_MIN = 320;
const PEEK_MAX = 560;
const PEEK_DEFAULT = 380;
const clampPeek = (w: number) => Math.min(PEEK_MAX, Math.max(PEEK_MIN, Math.round(w)));

function storedWidth(): number {
  if (typeof window === "undefined") return PEEK_DEFAULT;
  const saved = Number(localStorage.getItem(STORAGE.collectionPeek));
  return saved > 0 ? clampPeek(saved) : PEEK_DEFAULT;
}

/** The ONE record panel, shared by every lens - an in-layout flex sibling inside the ViewShell,
 * deliberately not a Sheet: all ui overlays share z-50, the artifact reader is already a Sheet,
 * and Base UI nests dialogs by trapping focus. Width is a viewer preference (localStorage), not
 * document state; the resize strip is the artifact-reader technique, with the delta form of the
 * math because this panel is not viewport-anchored. */
export function RecordPeek({
  data,
  row,
  canEdit,
  onSetCell,
  onRemoveRow,
  onClose,
  onNav,
  atStart,
  atEnd,
  index,
  count,
}: {
  data: Collection;
  row: CollectionRow;
  canEdit: boolean;
  onSetCell: (rowId: string, key: string, value: unknown) => void;
  onRemoveRow: (rowId: string) => void;
  onClose: () => void;
  onNav: (delta: 1 | -1) => void;
  atStart: boolean;
  atEnd: boolean;
  index: number;
  count: number;
}) {
  const [width, setWidth] = useState(storedWidth);
  const [dragging, setDragging] = useState(false);
  const widthRef = useRef(width);
  widthRef.current = width;
  const startRef = useRef<{ x: number; w: number } | null>(null);

  const commit = (w: number) => {
    setWidth(w);
    localStorage.setItem(STORAGE.collectionPeek, String(w));
  };
  const beginResize = (e: ReactPointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    startRef.current = { x: e.clientX, w: widthRef.current };
    setDragging(true);
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";
  };
  const resize = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId) || !startRef.current) return;
    setWidth(clampPeek(startRef.current.w + (startRef.current.x - e.clientX)));
  };
  const endResize = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
    e.currentTarget.releasePointerCapture(e.pointerId);
    setDragging(false);
    document.body.style.userSelect = "";
    document.body.style.cursor = "";
    commit(widthRef.current);
  };

  const primary = primaryField(data);
  const title = cellString(row.cells[primary.key]) ?? row.id;

  return (
    // Elevation: the raised step, shaped for a right-docked in-layout panel - a tonal step to
    // bg-card plus a directional left-cast shadow (a symmetric shadow would clip on the three
    // sides the shell's overflow-hidden owns). Open/close is INSTANT on purpose: the peek is a
    // flex sibling, and a width tween would reflow the lens scroller every frame - fighting
    // the gantt's same-frame scrollLeft compensation. The selection ring/edge in the lens
    // carries the state change; do not add a transition here.
    <aside
      aria-label="Record"
      className={
        /* token-ok: directional left-cast peek shadow - no symmetric elevation token fits a right-docked panel */ "bg-card relative flex min-h-0 shrink-0 flex-col overflow-hidden border-l shadow-[-12px_0_24px_-12px_rgb(0_0_0/0.2)] dark:shadow-[-12px_0_24px_-12px_rgb(0_0_0/0.55)]"
      }
      style={{ width, transition: dragging ? "none" : undefined }}
    >
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize record panel"
        aria-valuenow={width}
        aria-valuemin={PEEK_MIN}
        aria-valuemax={PEEK_MAX}
        tabIndex={0}
        onPointerDown={beginResize}
        onPointerMove={resize}
        onPointerUp={endResize}
        onPointerCancel={endResize}
        onDoubleClick={() => commit(PEEK_DEFAULT)}
        onKeyDown={(e) => {
          if (e.key === "ArrowLeft") commit(clampPeek(widthRef.current + 32));
          if (e.key === "ArrowRight") commit(clampPeek(widthRef.current - 32));
        }}
        className="hover:bg-primary/30 focus-visible:ring-ring absolute inset-y-0 left-0 z-10 w-1.5 cursor-col-resize touch-none outline-none select-none transition-[background-color] duration-150 focus-visible:ring-2"
      />
      <header className="flex h-(--strip-h) shrink-0 items-center gap-1 border-b px-4">
        <h3 className="line-clamp-1 min-w-0 flex-1 text-body-sm-regular font-medium">{title}</h3>
        <IconButton
          variant="ghost"
          size="sm"
          onClick={() => onNav(-1)}
          disabled={atStart}
          aria-label="Previous record"
          icon={<Icon icon={ChevronUp} />}
        />
        <IconButton
          variant="ghost"
          size="sm"
          onClick={() => onNav(1)}
          disabled={atEnd}
          aria-label="Next record"
          icon={<Icon icon={ChevronDown} />}
        />
        <IconButton
          variant="ghost"
          size="sm"
          onClick={onClose}
          aria-label="Close record"
          icon={<Icon icon={X} />}
        />
      </header>
      {/* Keyed by row so uncontrolled editors (defaultValue) remount when ↑/↓ moves records. */}
      <div
        key={row.id}
        className="scrollbar-thin min-h-0 flex-1 space-y-4 overflow-y-auto px-4 py-4"
      >
        {data.fields.map((f) => {
          const Icon = TYPE_ICON[f.type];
          return (
            <div key={f.key} className="min-w-0">
              <div className="text-muted-foreground flex items-center gap-1.5 text-caption-md-regular font-medium tracking-wide uppercase">
                <Icon aria-hidden className="size-3 shrink-0 opacity-70" />
                {f.label}
              </div>
              <div className="mt-1.5 text-body-sm-regular leading-relaxed break-words">
                {canEdit ? (
                  <CellEditor
                    field={f}
                    value={row.cells[f.key]}
                    onChange={(value) => onSetCell(row.id, f.key, value)}
                  />
                ) : (
                  <Cell field={f} value={row.cells[f.key]} />
                )}
              </div>
            </div>
          );
        })}
      </div>
      <footer className="flex h-(--footer-h) shrink-0 items-center justify-between border-t px-4">
        <MetaChip className="tabular-nums">
          {index} of {count}
        </MetaChip>
        {canEdit && (
          <Button
            variant="danger-outline"
            size="sm"
            stretch="auto"
            onClick={() => {
              onClose();
              onRemoveRow(row.id);
            }}
            icon={<Icon icon={Trash2} />}
            label="Delete row"
          />
        )}
      </footer>
    </aside>
  );
}
