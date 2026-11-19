/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import type { MutableRefObject } from "react";
import {
  primaryField,
  type Collection,
  type CollectionRow,
  type CollectionView,
} from "~/lib/collection";
import { Cell } from "../cells/cell";
import { FieldChipRow } from "../cells/field-chip";
import { PressSurface, pressOnKey } from "../core/press";
import { cardFields } from "../core/view-config";

/** Cards for one bucket. The chips are the VIEW's visible fields (minus what the board shows
 * structurally) - curated in the Fields menu, identical in meaning to the table's columns.
 * A card click opens the shared record peek; `draggingRef` keeps the click that ends a drag
 * from doubling as an open. */
export function CardList({
  data,
  view,
  rows,
  exclude,
  draggable,
  selectedRowId,
  onSelectRow,
  draggingRef,
}: {
  data: Collection;
  view: CollectionView;
  rows: CollectionRow[];
  exclude?: string | null;
  draggable: boolean;
  selectedRowId?: string | null;
  onSelectRow?: (rowId: string) => void;
  draggingRef?: MutableRefObject<boolean>;
}) {
  const primary = primaryField(data);
  const metas = cardFields(data, view, [view.group_by, exclude]);
  return (
    <ul className="space-y-2">
      {rows.map((row) =>
        draggable ? (
          <SortableCard
            key={row.id}
            row={row}
            selected={selectedRowId === row.id}
            onClick={() => {
              if (!draggingRef?.current) onSelectRow?.(row.id);
            }}
          >
            <CardBody data={data} row={row} primaryKey={primary.key} metas={metas} />
          </SortableCard>
        ) : (
          <li
            key={row.id}
            className="min-w-0 [content-visibility:auto] [contain-intrinsic-size:auto_5rem]"
          >
            <PressSurface
              onPress={onSelectRow ? () => onSelectRow(row.id) : undefined}
              selected={selectedRowId === row.id}
              className={`bg-card min-w-0 overflow-hidden rounded-md p-3.5 ring-inset transition-[background-color,box-shadow] duration-150 ${
                selectedRowId === row.id
                  ? "ring-2 ring-primary/40"
                  : `ring-1 ring-foreground/10${onSelectRow ? " hover:ring-foreground/25" : ""}`
              } ${onSelectRow ? "cursor-pointer" : ""}`}
            >
              <CardBody data={data} row={row} primaryKey={primary.key} metas={metas} />
            </PressSurface>
          </li>
        ),
      )}
      {rows.length === 0 && (
        <li className="text-muted-foreground px-3.5 py-3 text-caption-md-regular">Nothing here.</li>
      )}
    </ul>
  );
}

/** A sortable card: within-column reorder AND cross-column moves ride one hook. Mid-drag the
 * in-place card fades to a placeholder - the DragOverlay in board-view carries the visual. */
function SortableCard({
  row,
  selected,
  onClick,
  children,
}: {
  row: CollectionRow;
  selected: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: row.id,
    data: { type: "card" },
  });
  // The sortable node is the card inside the list item: dnd-kit's attributes make it a focusable
  // button, Space lifts it (the keyboard sensor) and Enter opens it, as a click does.
  return (
    <li className="min-w-0">
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
        className={`bg-card focus-visible:ring-ring min-w-0 cursor-grab touch-none overflow-hidden rounded-md p-3.5 ring-inset outline-none transition-[background-color,box-shadow] duration-150 focus-visible:ring-2 ${
          isDragging
            ? "ring-1 ring-foreground/10 opacity-30"
            : selected
              ? "ring-2 ring-primary/40"
              : "ring-1 ring-foreground/10 hover:ring-foreground/25"
        }`}
      >
        {children}
      </div>
    </li>
  );
}

/** Named export: the DragOverlay renders the same body while the source card placeholders. */
export function CardBody({
  data,
  row,
  primaryKey,
  metas,
}: {
  data: Collection;
  row: CollectionRow;
  primaryKey: string;
  metas: ReturnType<typeof cardFields>;
}) {
  const primary = primaryField(data);
  return (
    <>
      <Cell field={primary} value={row.cells[primaryKey]} primary />
      <FieldChipRow fields={metas} cells={row.cells} className="mt-1.5" />
    </>
  );
}
