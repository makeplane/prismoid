/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Who owns a key press on the inbox. The list's keyboard (↑/↓ move the selection, Enter opens it)
// listens on the window, so it must stand aside whenever focus sits in a control with keys of its
// own: a text field, a toolbar button, an open menu, the pane's resize handle. It acts only on the
// page itself or on a notification row.

/** Marks a notification row: focus on one still leaves the keys to the list. */
export const ROW_ATTR = "data-inbox-row";

const OWN_KEYS =
  'input, textarea, select, button, a[href], [contenteditable=""], [contenteditable="true"], ' +
  '[role="button"], [role="link"], [role^="menuitem"], [role="tab"], [role="separator"], ' +
  '[tabindex]:not([tabindex="-1"])';

export function listOwnsKeys(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return true;
  if (target.closest(`[${ROW_ATTR}]`)) return true;
  return target.closest(OWN_KEYS) === null;
}
