/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The topbar's extension points: a route projects page-scoped chrome into the header
// without the shell knowing any route's shape. Two slots - "end" (the right cluster:
// search, actions, telemetry) and "start" (immediately beside the breadcrumb - the
// inbox's Linear-style pane controls live there). Unused slots render empty
// (empty:hidden keeps them out of the header's flex gap).

import { useEffect, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

export const TOPBAR_SLOT_ID = "topbar-slot";
export const TOPBAR_START_SLOT_ID = "topbar-slot-start";

/** Route-scoped: while mounted, the SHELL HEADER steps aside entirely - the route brings
 *  its own header(s). The one user is the task page's artifact mode (the task
 *  page's right side): the artifact deserves its own full-height plane from the very top
 *  of the window, so the chat renders its own band and the shared bar yields. Same
 *  SPA-only pragmatism as the portal below; cleanup restores the header on unmount. */
export function TopbarYield() {
  useEffect(() => {
    const el = document.querySelector<HTMLElement>("[data-shell-header]");
    if (!el) return;
    el.style.display = "none";
    return () => {
      el.style.display = "";
    };
  }, []);
  return null;
}

/** Mounts children into a shell-header slot. SPA-only by design: the slot element exists
 *  once the shell painted; until then (and in node renders) this is a no-op. */
export function TopbarPortal({
  side = "end",
  children,
}: {
  side?: "start" | "end";
  children: ReactNode;
}) {
  const [el, setEl] = useState<HTMLElement | null>(null);
  useEffect(() => {
    setEl(document.getElementById(side === "end" ? TOPBAR_SLOT_ID : TOPBAR_START_SLOT_ID));
  }, [side]);
  return el ? createPortal(children, el) : null;
}
