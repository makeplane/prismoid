/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { closestCenter, DndContext, type DragEndEvent } from "@dnd-kit/core";
import { rectSortingStrategy, SortableContext, useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { useMemo, useRef, useState } from "react";
import { IconTile, ImageChip, artifactIcon } from "~/components/kit";
import {
  cellString,
  primaryField,
  type Collection,
  type CollectionField,
  type CollectionRow,
  type CollectionView,
} from "~/lib/collection";
import { imageAllowed } from "~/lib/image-policy";
import { Cell } from "../cells/cell";
import { FieldChipRow } from "../cells/field-chip";
import type { MoveAnchor } from "../core/edit-state";
import { PressSurface, pressOnKey } from "../core/press";
import { cardFields } from "../core/view-config";
import { moveAnchorFor, useCollectionSensors } from "../dnd";

/** The gallery lens, v4 - cover-image cards over the projected rows. Tiles reorder by
 * whole-tile drag (no handle: tiles are large; the 4px activation distance keeps clicks
 * opening the peek), hidden under an active sort - manual order is meaningless there. A cover
 * loads only from an origin the image policy allows (a model wrote the URL); any other is a
 * quiet tile with a chip that opens it on click, and a dead image degrades to a quiet tile. */
export function GalleryView({
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
  const coverKey = view.cover_field ?? undefined;
  const metas = cardFields(data, view, [coverKey]);
  const canReorder = !!onMoveRow && view.sort.length === 0;
  const sensors = useCollectionSensors();
  const draggingRef = useRef(false);
  const rowIds = useMemo(() => rows.map((r) => r.id), [rows]);

  const onDragEnd = (event: DragEndEvent) => {
    setTimeout(() => {
      draggingRef.current = false;
    }, 0);
    const { active, over } = event;
    if (!over || !onMoveRow) return;
    const anchor = moveAnchorFor(rowIds, String(active.id), String(over.id));
    if (anchor) onMoveRow(String(active.id), anchor);
  };

  const grid = (
    <ul className="grid gap-3 p-3 sm:grid-cols-2 lg:grid-cols-3">
      {rows.map((row) =>
        canReorder ? (
          <SortableTile
            key={row.id}
            row={row}
            primary={primary}
            coverKey={coverKey}
            metas={metas}
            selected={selectedRowId === row.id}
            onClick={() => {
              if (!draggingRef.current) onSelectRow?.(row.id);
            }}
          />
        ) : (
          <li
            key={row.id}
            className="[content-visibility:auto] [contain-intrinsic-size:auto_14rem]"
          >
            <PressSurface
              onPress={onSelectRow ? () => onSelectRow(row.id) : undefined}
              selected={selectedRowId === row.id}
              className={`bg-card h-full overflow-hidden rounded-lg ring-inset transition-[background-color,box-shadow] duration-150 ${
                selectedRowId === row.id
                  ? "ring-2 ring-primary/40"
                  : `ring-1 ring-foreground/10${onSelectRow ? " hover:ring-foreground/25" : ""}`
              } ${onSelectRow ? "cursor-pointer" : ""}`}
            >
              <TileBody row={row} primary={primary} coverKey={coverKey} metas={metas} />
            </PressSurface>
          </li>
        ),
      )}
    </ul>
  );

  return canReorder ? (
    <DndContext
      sensors={sensors}
      collisionDetection={closestCenter}
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
      <SortableContext items={rowIds} strategy={rectSortingStrategy}>
        {grid}
      </SortableContext>
    </DndContext>
  ) : (
    grid
  );
}

function SortableTile({
  row,
  primary,
  coverKey,
  metas,
  selected,
  onClick,
}: {
  row: CollectionRow;
  primary: CollectionField;
  coverKey?: string;
  metas: CollectionField[];
  selected: boolean;
  onClick: () => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: row.id,
  });
  // The sortable node is the tile inside the list item: dnd-kit's attributes make it a focusable
  // button, Space lifts it (the keyboard sensor) and Enter opens it, as a click does.
  return (
    <li className="h-full">
      <div
        ref={setNodeRef}
        {...attributes}
        {...listeners}
        role="button"
        aria-current={selected || undefined}
        onClick={onClick}
        onKeyDown={(e) => {
          listeners?.onKeyDown?.(e);
          pressOnKey(onClick, ["Enter"])(e);
        }}
        style={{ transform: CSS.Transform.toString(transform), transition }}
        className={`bg-card focus-visible:ring-ring h-full cursor-grab touch-none overflow-hidden rounded-lg ring-inset outline-none transition-[background-color,box-shadow] duration-150 focus-visible:ring-2 ${
          isDragging
            ? "ring-foreground/10 z-10 opacity-80 shadow-raised-300 ring-1"
            : selected
              ? "ring-2 ring-primary/40"
              : "ring-1 ring-foreground/10 hover:ring-foreground/25"
        }`}
      >
        <TileBody row={row} primary={primary} coverKey={coverKey} metas={metas} />
      </div>
    </li>
  );
}

function TileBody({
  row,
  primary,
  coverKey,
  metas,
}: {
  row: CollectionRow;
  primary: CollectionField;
  coverKey?: string;
  metas: CollectionField[];
}) {
  return (
    <>
      <Cover url={coverKey ? cellString(row.cells[coverKey]) : null} />
      <div className="space-y-1.5 p-3.5">
        <Cell field={primary} value={row.cells[primary.key]} primary />
        <FieldChipRow fields={metas} cells={row.cells} />
      </div>
    </>
  );
}

function Cover({ url }: { url: string | null }) {
  const [failed, setFailed] = useState(false);
  if (!url || failed || !imageAllowed(url)) {
    return (
      <div className="bg-muted/40 grid aspect-video place-content-center justify-items-center gap-2">
        <IconTile icon={artifactIcon("collection")} />
        {url && !failed && <ImageChip src={url} alt="Open cover image" />}
      </div>
    );
  }
  return (
    <img
      src={url}
      alt=""
      loading="lazy"
      referrerPolicy="no-referrer"
      onError={() => setFailed(true)}
      className="aspect-video w-full object-cover"
    />
  );
}
