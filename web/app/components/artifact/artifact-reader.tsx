/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Drawer as BaseDrawer } from "@base-ui/react/drawer";
import { Maximize2, X } from "lucide-react";
import { useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { Link } from "react-router";
import { ArtifactView } from "~/components/artifact/artifact-view";
import { Icon } from "@makeplane/propel/components/icon";
import {
  Drawer,
  DrawerBackdrop,
  DrawerTitle,
  DrawerViewport,
} from "@makeplane/propel/components/drawer";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { STORAGE } from "~/lib/storage";

/** The artifact reader: the resizable document pane, opened from a transcript card, the rail,
 *  or a project's shelf. Full screen (/artifacts/:id) is the reader's escalation, one click away.
 *  No header band of its own: the artifact's title row IS the header - the pane's controls
 *  (full screen, close) ride ArtifactView's action cluster so everything shares one baseline. */
export function ArtifactReader({
  artifactId,
  onClose,
  kindHint,
  onArtifactChanged,
  readOnly = false,
}: {
  artifactId: string | null;
  onClose: () => void;
  /** The artifact's wire type when the opener knows it - wide kinds open at a wider default. */
  kindHint?: string | null;
  /** A save/restore minted a new head - hosts keying on the open id repoint here. */
  onArtifactChanged?: (artifactId: string) => void;
  /** The reader may not write (a Project viewer): ArtifactView offers no edit, restore or label. */
  readOnly?: boolean;
}) {
  return (
    <PeekSheet open={!!artifactId} onOpenChange={(o) => !o && onClose()} kindHint={kindHint}>
      <div className="sr-only">
        <DrawerTitle>Artifact</DrawerTitle>
      </div>
      {/* flex-1 min-h-0 gives ArtifactView a definite box; the HOST is the scroller (not the
          sheet), so a document's overflow scrolls inside the padded box and pb-6 is honored
          below the last block - padding on an overflow-visible box would be lost past its
          edge. (Every shipped kind flows like a document now - registry.test.tsx pins it.) */}
      {/* overflow-x-hidden: the pane never scrolls sideways - wide content scrolls inside
          its own overflow-x-auto box (the same law as the task page's split pane). */}
      <div className="scrollbar-thin min-h-0 min-w-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-contain px-4 pt-4 pb-6">
        {artifactId && (
          <ArtifactView
            artifactId={artifactId}
            onArtifactChanged={onArtifactChanged}
            readOnly={readOnly}
            hostActions={
              <>
                <IconButton
                  nativeButton={false}
                  variant="ghost"
                  size="sm"
                  icon={<Icon icon={Maximize2} />}
                  aria-label="Full screen"
                  render={<Link to={`/artifacts/${artifactId}`} />}
                />
                <IconButton
                  variant="ghost"
                  size="sm"
                  icon={<Icon icon={X} />}
                  aria-label="Close"
                  onClick={onClose}
                />
              </>
            }
          />
        )}
      </div>
    </PeekSheet>
  );
}

// ── the artifact pane: a resizable document drawer ────────────────────────────────────────────

const DRAWER_WIDTH_KEY = STORAGE.drawerWidth;
const DRAWER_MIN_PX = 400;
// Kinds whose natural shape is a board/grid, not a reading column - their DEFAULT is wider.
// A width the user chose (and persisted) always wins over any default.
const WIDE_KINDS = new Set(["collection", "spreadsheet", "dataset"]);
// The viewport ceiling never drops below the floor: a 0-sized window (an embedded pane's first
// layout pass, a background prerender) once collapsed the whole sheet to 0px - and since the
// sheet mounts once per route, nothing ever recovered it. min(max(w, floor), max(ceiling, floor))
// keeps the floor authoritative whatever the window claims.
const clampWidth = (w: number) =>
  Math.round(
    Math.min(Math.max(w, DRAWER_MIN_PX), Math.max(window.innerWidth * 0.95, DRAWER_MIN_PX)),
  );
const defaultWidth = (kindHint?: string | null) =>
  clampWidth(
    WIDE_KINDS.has(kindHint ?? "")
      ? Math.min(1200, window.innerWidth * 0.92)
      : Math.min(896, window.innerWidth * 0.9),
  );

/** A document pane whose width the reader owns: drag the left edge (arrow keys work on it too;
 *  double-click resets). Width is an element style and it persists, so every artifact opens at
 *  the width you last chose. State lives here, not in the page, so a drag re-renders only the
 *  drawer. Propel's DrawerPanel comes in fixed widths only, so the popup is Base UI's inside
 *  Propel's root, backdrop and viewport (the blocks sidebar's mobile drawer does the same).
 *  Exported as the ONE peek shell: the bots messenger's task peek rides the same drawer (and
 *  the same remembered width), so "look at it in place" feels identical everywhere. */
export function PeekSheet({
  open,
  onOpenChange,
  kindHint,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  kindHint?: string | null;
  children: ReactNode;
}) {
  const [width, setWidth] = useState(() => {
    if (typeof window === "undefined") return 896; // SSR never opens it; any number serves
    const saved = Number(localStorage.getItem(DRAWER_WIDTH_KEY));
    return saved > 0 ? clampWidth(saved) : defaultWidth(kindHint);
  });
  const widthRef = useRef(width);
  widthRef.current = width;

  const commit = (w: number) => {
    setWidth(w);
    localStorage.setItem(DRAWER_WIDTH_KEY, String(w));
  };

  const beginResize = (e: ReactPointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";
  };
  const resize = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
    setWidth(clampWidth(window.innerWidth - e.clientX));
  };
  const endResize = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
    e.currentTarget.releasePointerCapture(e.pointerId);
    document.body.style.userSelect = "";
    document.body.style.cursor = "";
    commit(widthRef.current);
  };

  return (
    <Drawer open={open} onOpenChange={onOpenChange} modal swipeDirection="right">
      <BaseDrawer.Portal>
        <DrawerBackdrop />
        <BaseDrawer.Viewport render={<DrawerViewport variant="overlay" />}>
          {/* Only transform transitions (the slide), so a drag's width never rubber-bands. */}
          <BaseDrawer.Popup
            className="border-subtle bg-surface-1 shadow-direction-left text-popover-foreground pointer-events-auto fixed inset-y-0 right-0 z-50 flex flex-col overflow-hidden border-l text-caption-md-regular/relaxed outline-none transition-transform duration-200 data-ending-style:translate-x-full data-starting-style:translate-x-full"
            style={{ width, maxWidth: "95vw" }}
          >
            <div
              role="separator"
              data-base-ui-swipe-ignore
              aria-orientation="vertical"
              aria-label="Resize artifact pane"
              aria-valuenow={width}
              aria-valuemin={DRAWER_MIN_PX}
              tabIndex={0}
              onPointerDown={beginResize}
              onPointerMove={resize}
              onPointerUp={endResize}
              onPointerCancel={endResize}
              onDoubleClick={() => commit(defaultWidth())}
              onKeyDown={(e) => {
                if (e.key === "ArrowLeft") commit(clampWidth(widthRef.current + 32));
                if (e.key === "ArrowRight") commit(clampWidth(widthRef.current - 32));
              }}
              className="hover:bg-accent focus-visible:ring-ring absolute inset-y-0 left-0 z-10 hidden w-1.5 cursor-col-resize touch-none outline-none select-none focus-visible:ring-2 sm:block"
            />
            {/* Content: a mouse press in here selects text instead of swiping the drawer shut. */}
            <BaseDrawer.Content className="flex min-h-0 flex-1 flex-col">
              {children}
            </BaseDrawer.Content>
          </BaseDrawer.Popup>
        </BaseDrawer.Viewport>
      </BaseDrawer.Portal>
    </Drawer>
  );
}
