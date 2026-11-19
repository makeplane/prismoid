/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Download, Redo2, Undo2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { EmptyState } from "~/components/kit";
import {
  cellDateMs,
  cellString,
  parseCollection,
  type Collection,
  type CollectionRow,
  type CollectionView,
} from "~/lib/collection";
import type { RendererProps } from "../registry";
import { BoardView } from "./board/board-view";
import { CalendarView } from "./calendar/calendar-view";
import { DAY_MS, isoDate } from "./core/date-math";
import {
  buildContent,
  mintRowId,
  moveRowEdit,
  pushEdit,
  type Edit,
  type MoveAnchor,
} from "./core/edit-state";
import { useRevise } from "./core/use-revise";
import { applyView } from "./core/view-config";
import { GalleryView } from "./gallery/gallery-view";
import { GanttView } from "./gantt/gantt-view";
import { ListView } from "./list/list-view";
import { RecordPeek } from "./peek/record-peek";
import { TableView } from "./table/table-view";
import { ToolbarSlotContext } from "./toolbar/slot";
import { ViewToolbar } from "./toolbar/view-toolbar";
import { ViewShell } from "./view-shell";

/**
 * The collection renderer - DIRECT manipulation over one working copy. There is no
 * separate edit mode: when the viewer can save, the board drags, the record peek edits, columns
 * and tables grow rows, and views are added or removed in place. Every gesture stages an
 * invertible command; the dirty pill (undo · redo · save · discard) turns the staged set into
 * the lineage's next revision, with a 409 rebasing the same commands onto whatever landed.
 *
 * Collections v3: `applyView` (filter → sort → visible fields) is the one projection every lens
 * consumes, the ViewShell is the one surface and the one scroller, and the RecordPeek is the
 * one record panel - selection, ↑/↓ order, and Esc live here so the lenses stay presentational.
 */
export default function CollectionRenderer({
  artifact,
  download,
  text,
  onArtifactChanged,
  fill,
}: RendererProps) {
  const parsed = useMemo(() => parseCollection(text ?? ""), [text]);
  const [selected, setSelected] = useState(0);
  const [edits, setEdits] = useState<Edit[]>([]);
  const [undone, setUndone] = useState<Edit[]>([]);
  const [selectedRowId, setSelectedRowId] = useState<string | null>(null);
  const [slotEl, setSlotEl] = useState<HTMLElement | null>(null);
  // The shell's ONE scroll element - the gantt viewport (and the table's stickies) hang off it.
  const shellScrollRef = useRef<HTMLDivElement | null>(null);

  const base = parsed.ok ? parsed.data : null;
  const working = useMemo(() => (base ? buildContent(base, edits) : null), [base, edits]);
  const { save, saving } = useRevise(artifact, base ?? ({} as Collection), (id) => {
    setEdits([]);
    setUndone([]);
    onArtifactChanged?.(id);
  });
  const canEdit = !!onArtifactChanged;

  const view = working?.views[Math.min(selected, working.views.length - 1)];
  const projected = useMemo(
    () => (working && view ? applyView(working, view) : { rows: [], fields: [] }),
    [working, view],
  );

  // Selection follows the PROJECTED order (layout-agnostic; the board's grouped order
  // intentionally differs). It clears on view switch and when the row leaves the projection.
  useEffect(() => setSelectedRowId(null), [selected]);
  useEffect(() => {
    if (selectedRowId && !projected.rows.some((r) => r.id === selectedRowId)) {
      setSelectedRowId(null);
    }
  }, [projected.rows, selectedRowId]);

  const selectedRow = projected.rows.find((r) => r.id === selectedRowId) ?? null;

  // Esc closes the peek, ↑/↓ walk the projection - the inbox recipe, input-guarded.
  useEffect(() => {
    if (!selectedRow) return;
    function onKey(e: KeyboardEvent) {
      if (e.target instanceof HTMLElement) {
        const tag = e.target.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || e.target.isContentEditable)
          return;
      }
      if (e.key === "Escape") {
        setSelectedRowId(null);
        return;
      }
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      e.preventDefault();
      const idx = projected.rows.findIndex((r) => r.id === selectedRowId);
      const next =
        e.key === "ArrowDown"
          ? projected.rows[Math.min(idx + 1, projected.rows.length - 1)]
          : projected.rows[Math.max(idx - 1, 0)];
      if (next) setSelectedRowId(next.id);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  if (!parsed.ok || !working || !view) {
    return (
      <div className="ring-foreground/10 rounded-xl p-4 shadow-raised-100 ring-1">
        <EmptyState
          compact
          title="This collection can't be read"
          description={!parsed.ok ? parsed.reason : "empty"}
          action={
            <Button
              nativeButton={false}
              render={<a href={download.url} download={download.filename} />}
              variant="secondary"
              size="sm"
              stretch="auto"
              icon={<Icon icon={Download} />}
              label="Download the file"
            />
          }
        />
      </div>
    );
  }

  const stage = (edit: Edit) => {
    setEdits((prev) => [...prev, edit]);
    setUndone([]);
  };
  // A gesture may need several commands (a bar move touches two cells; a cross-column card
  // drop is a cell edit AND a reorder) - staged as ONE batch, ONE undo step, ONE pill count.
  // Adapters pass null for no-op halves; nothing real ⇒ nothing staged.
  const stageBatch = (parts: (Edit | null)[], label?: string) => {
    const real = parts.filter((e): e is Edit => e != null);
    if (real.length === 0) return;
    stage(real.length === 1 ? real[0] : { kind: "batch", label, edits: real });
  };
  const setCell = (rowId: string, key: string, value: unknown) => {
    const prev = working.rows.find((row) => row.id === rowId)?.cells[key] ?? null;
    stage({ kind: "set_cell", rowId, key, value, prev });
  };
  const addRow = (cells: Record<string, unknown> = {}) => {
    stage({
      kind: "add_row",
      row: { id: mintRowId(working), cells },
      index: working.rows.length,
    });
  };
  const removeRow = (rowId: string) => {
    const index = working.rows.findIndex((row) => row.id === rowId);
    const row = working.rows[index];
    if (row) stage({ kind: "remove_row", row, index });
  };
  // Drag adapters - lenses emit semantics (an anchor, a day delta), this file mints commands.
  const moveRow = (rowId: string, anchor: MoveAnchor) => {
    const edit = moveRowEdit(working.rows, rowId, anchor);
    if (edit) stage(edit);
  };
  const dropCard = (rowId: string, groupValue: unknown, anchor: MoveAnchor | null) => {
    const groupKey = view.group_by ?? "";
    const row = working.rows.find((r) => r.id === rowId);
    const current = row?.cells[groupKey];
    const changed = !(
      cellString(current) === cellString(groupValue) ||
      (current == null && groupValue == null)
    );
    stageBatch(
      [
        changed
          ? { kind: "set_cell", rowId, key: groupKey, value: groupValue, prev: current ?? null }
          : null,
        anchor ? moveRowEdit(working.rows, rowId, anchor) : null,
      ],
      "Move card",
    );
  };
  const shiftDateEdit = (
    rowId: string,
    key: string | null | undefined,
    deltaDays: number,
  ): Edit | null => {
    if (!key) return null;
    const prev = working.rows.find((r) => r.id === rowId)?.cells[key];
    const ms = cellDateMs(prev);
    return ms == null
      ? null
      : { kind: "set_cell", rowId, key, value: isoDate(ms + deltaDays * DAY_MS), prev };
  };
  const moveBar = (rowId: string, deltaDays: number) => {
    const sk = view.start_field;
    const ek = view.end_field;
    stageBatch(
      [shiftDateEdit(rowId, sk, deltaDays), sk === ek ? null : shiftDateEdit(rowId, ek, deltaDays)],
      "Move bar",
    );
  };
  const resizeBar = (rowId: string, edge: "start" | "end", deltaDays: number) =>
    stageBatch([
      shiftDateEdit(rowId, edge === "start" ? view.start_field : view.end_field, deltaDays),
    ]);
  // The one adapter that turns a layout/toolbar patch into the invertible command - layouts and
  // menus never build Edit objects. coalesce folds a drag burst into a single undo step.
  const updateView = (patch: Partial<CollectionView>, opts?: { coalesce?: boolean }) => {
    const next = { ...view, ...patch };
    setEdits((prev) =>
      pushEdit(
        prev,
        {
          kind: "update_view",
          index: selected,
          prev: structuredClone(view),
          next,
        },
        opts?.coalesce,
      ),
    );
    setUndone([]);
  };
  const undo = () =>
    setEdits((prev) => {
      const last = prev.at(-1);
      if (last) setUndone((u) => [...u, last]);
      return prev.slice(0, -1);
    });
  const redo = () =>
    setUndone((prev) => {
      const last = prev.at(-1);
      if (last) setEdits((e) => [...e, last]);
      return prev.slice(0, -1);
    });

  const onNav = (delta: 1 | -1) => {
    const idx = projected.rows.findIndex((r) => r.id === selectedRowId);
    const next = projected.rows[idx + delta];
    if (next) setSelectedRowId(next.id);
  };
  const selectedIdx = projected.rows.findIndex((r) => r.id === selectedRowId);
  const hidden = working.rows.length - projected.rows.length;
  const empty =
    projected.rows.length === 0
      ? {
          title: "No rows",
          description: hidden > 0 ? `${hidden} hidden by filters` : undefined,
        }
      : null;

  return (
    <div className={fill ? "flex h-full min-h-0 flex-col gap-3" : "space-y-3"}>
      <ViewToolbar
        data={working}
        view={view}
        selected={selected}
        canEdit={canEdit}
        projectedCount={projected.rows.length}
        stagedCount={edits.length}
        onSelect={setSelected}
        onRemoveView={(v, i) => {
          stage({ kind: "remove_view", view: v, index: i });
          setSelected(Math.max(0, i - 1));
        }}
        onAddView={(v) => {
          stage({ kind: "add_view", view: v, index: working.views.length });
          setSelected(working.views.length);
        }}
        onUpdateView={updateView}
        slotRef={setSlotEl}
      />

      <ToolbarSlotContext.Provider value={slotEl}>
        <ViewShell
          ariaLabel={`${view.name} ${view.layout}`}
          empty={empty}
          fill={fill}
          scrollRef={shellScrollRef}
          peek={
            selectedRow && (
              <RecordPeek
                data={working}
                row={selectedRow}
                canEdit={canEdit}
                onSetCell={setCell}
                onRemoveRow={removeRow}
                onClose={() => setSelectedRowId(null)}
                onNav={onNav}
                atStart={selectedIdx <= 0}
                atEnd={selectedIdx >= projected.rows.length - 1}
                index={selectedIdx + 1}
                count={projected.rows.length}
              />
            )
          }
        >
          <ViewLayoutSwitch
            data={working}
            view={view}
            rows={projected.rows}
            canEdit={canEdit}
            setCell={setCell}
            addRow={addRow}
            selectedRowId={selectedRowId}
            onSelectRow={setSelectedRowId}
            scrollerRef={shellScrollRef}
            updateView={updateView}
            dropCard={dropCard}
            moveRow={moveRow}
            moveBar={moveBar}
            resizeBar={resizeBar}
          />
        </ViewShell>
      </ToolbarSlotContext.Provider>

      {edits.length > 0 && (
        <div className="ring-foreground/10 bg-card/90 sticky bottom-2 z-20 flex items-center gap-2 rounded-xl p-2 shadow-raised-300 ring-1 backdrop-blur-sm">
          <span className="text-muted-foreground px-2 text-caption-md-regular tabular-nums">
            {edits.length} change{edits.length === 1 ? "" : "s"}
          </span>
          <IconButton
            variant="ghost"
            size="sm"
            icon={<Icon icon={Undo2} />}
            onClick={undo}
            aria-label="Undo"
          />
          <IconButton
            variant="ghost"
            size="sm"
            icon={<Icon icon={Redo2} />}
            onClick={redo}
            disabled={undone.length === 0}
            aria-label="Redo"
          />
          <span className="flex-1" />
          <Button
            variant="ghost"
            size="sm"
            stretch="auto"
            label="Discard"
            onClick={() => {
              setEdits([]);
              setUndone([]);
            }}
            disabled={saving}
          />
          <Button
            variant="primary"
            size="sm"
            stretch="auto"
            label={saving ? "Saving…" : "Save revision"}
            onClick={() => void save(edits)}
            disabled={saving}
          />
        </div>
      )}
    </div>
  );
}

function ViewLayoutSwitch({
  data,
  view,
  rows,
  canEdit,
  setCell,
  addRow,
  selectedRowId,
  onSelectRow,
  scrollerRef,
  updateView,
  dropCard,
  moveRow,
  moveBar,
  resizeBar,
}: {
  data: Collection;
  view: CollectionView;
  rows: CollectionRow[];
  canEdit: boolean;
  setCell: (rowId: string, key: string, value: unknown) => void;
  addRow: (cells?: Record<string, unknown>) => void;
  selectedRowId: string | null;
  onSelectRow: (rowId: string) => void;
  scrollerRef: RefObject<HTMLDivElement | null>;
  updateView: (patch: Partial<CollectionView>, opts?: { coalesce?: boolean }) => void;
  dropCard: (rowId: string, groupValue: unknown, anchor: MoveAnchor | null) => void;
  moveRow: (rowId: string, anchor: MoveAnchor) => void;
  moveBar: (rowId: string, deltaDays: number) => void;
  resizeBar: (rowId: string, edge: "start" | "end", deltaDays: number) => void;
}) {
  switch (view.layout) {
    case "board":
      return (
        <BoardView
          data={data}
          view={view}
          rows={rows}
          onDropCard={canEdit ? dropCard : undefined}
          onAddRow={
            canEdit && view.group_by
              ? (groupValue) => addRow({ [view.group_by as string]: groupValue })
              : undefined
          }
          selectedRowId={selectedRowId}
          onSelectRow={onSelectRow}
          onUpdateView={canEdit ? updateView : undefined}
        />
      );
    case "list":
      return (
        <ListView
          data={data}
          view={view}
          rows={rows}
          selectedRowId={selectedRowId}
          onSelectRow={onSelectRow}
          onMoveRow={canEdit ? moveRow : undefined}
        />
      );
    case "gallery":
      return (
        <GalleryView
          data={data}
          view={view}
          rows={rows}
          selectedRowId={selectedRowId}
          onSelectRow={onSelectRow}
          onMoveRow={canEdit ? moveRow : undefined}
        />
      );
    case "gantt":
      return (
        <GanttView
          data={data}
          view={view}
          rows={rows}
          scrollerRef={scrollerRef}
          selectedRowId={selectedRowId}
          onSelectRow={onSelectRow}
          onMoveBar={canEdit ? moveBar : undefined}
          onResizeBar={canEdit ? resizeBar : undefined}
          onMoveRow={canEdit ? moveRow : undefined}
          onUpdateView={canEdit ? updateView : undefined}
        />
      );
    case "calendar":
      return (
        <CalendarView
          data={data}
          view={view}
          rows={rows}
          selectedRowId={selectedRowId}
          onSelectRow={onSelectRow}
          onSetCell={canEdit ? setCell : undefined}
        />
      );
    case "table":
      return (
        <TableView
          data={data}
          view={view}
          rows={rows}
          onAddRow={canEdit ? () => addRow() : undefined}
          selectedRowId={selectedRowId}
          onSelectRow={onSelectRow}
          onUpdateView={canEdit ? updateView : undefined}
          onMoveRow={canEdit ? moveRow : undefined}
        />
      );
    default:
      return (
        <div className="space-y-2 pt-2">
          <p className="text-muted-foreground px-3 text-caption-md-regular">
            This viewer doesn&apos;t render “{view.layout}” yet - showing the table.
          </p>
          <TableView
            data={data}
            view={view}
            rows={rows}
            onAddRow={canEdit ? () => addRow() : undefined}
            selectedRowId={selectedRowId}
            onSelectRow={onSelectRow}
            onUpdateView={canEdit ? updateView : undefined}
          />
        </div>
      );
  }
}
