/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { KeyboardCode, KeyboardSensor, PointerSensor, useSensor, useSensors } from "@dnd-kit/core";
import { sortableKeyboardCoordinates } from "@dnd-kit/sortable";
import type { MoveAnchor } from "./core/edit-state";

/** ONE sensor recipe for every dnd lens (board, calendar, table, list, gallery): 4px of
 * travel distinguishes a click (opens the peek) from a lift; the keyboard sensor keeps
 * sortable's built-in announcements. Only Space lifts, so Enter on a focused tile or card is
 * left to open it, as a click does. */
export function useCollectionSensors() {
  return useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
      keyboardCodes: KEYBOARD_CODES,
    }),
  );
}

const KEYBOARD_CODES = {
  start: [KeyboardCode.Space],
  cancel: [KeyboardCode.Esc],
  end: [KeyboardCode.Space, KeyboardCode.Enter],
};

/** Sortable drop → neighbor anchor, shared by table/list/gallery. Dragging DOWN lands after
 * `over` (anchor before over's successor, or after over when it is last); dragging UP lands
 * before `over`. Same id → null (nothing to stage). */
export function moveAnchorFor(ids: string[], activeId: string, overId: string): MoveAnchor | null {
  if (activeId === overId) return null;
  const from = ids.indexOf(activeId);
  const to = ids.indexOf(overId);
  if (from === -1 || to === -1) return null;
  if (from < to) {
    return to + 1 < ids.length ? { beforeId: ids[to + 1] } : { afterId: ids[to] };
  }
  return { beforeId: ids[to] };
}
