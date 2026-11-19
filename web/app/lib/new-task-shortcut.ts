/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The app-wide "new task" chord: Cmd+Shift+O (Ctrl+Shift+O off a Mac), the chord chat apps use
// for a new conversation. Cmd+N is the browser's new window and is left alone. Pure, so the
// sidebar's listener only asks and navigates.

/** The chord as the sidebar tooltip shows it, beside the sidebar's own ⌘B. */
export const NEW_TASK_SHORTCUT = "⌘⇧O";

type Chord = Pick<KeyboardEvent, "key" | "metaKey" | "ctrlKey" | "shiftKey" | "altKey">;

/** Is this keydown the new-task chord? Shift makes `key` "O"; Caps Lock may make it "o". */
export function isNewTaskShortcut(e: Chord): boolean {
  return (e.metaKey || e.ctrlKey) && e.shiftKey && !e.altKey && e.key.toLowerCase() === "o";
}
