/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { createContext, useContext } from "react";

/** The per-lens control slot: the toolbar renders a target element and publishes it through
 * this context; a lens (the gantt's zoom + Today) portals its controls in. Null when no toolbar
 * is mounted (tests, bare lens renders) - the lens then renders the same controls inline as its
 * permanent fallback, which the smoke tests rely on. */
export const ToolbarSlotContext = createContext<HTMLElement | null>(null);

export function useToolbarSlot(): HTMLElement | null {
  return useContext(ToolbarSlotContext);
}
