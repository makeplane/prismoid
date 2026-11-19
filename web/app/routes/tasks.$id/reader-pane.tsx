/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/**
 * ReaderPane - the artifact reader as a TRUE SPLIT: it PUSHES the transcript
 * aside instead of floating over it, so reading the artifact and skimming the thread are one
 * posture, and a live revision visibly lands beside the words that produced it.
 *
 * ## Where it lives (v4 - all three regions visible, the artifact's OWN plane)
 *   Wide screens (xl+) only, inside the shell's inset BESIDE the sidebar - but from the
 *   very top of the window: the shared topbar yields (`TopbarYield`) while this pane is
 *   mounted, the chat renders its own h-14 band (← · title · chrome · Progress-modal
 *   button), and ArtifactView's title row is this pane's header. Narrow screens keep the
 *   overlay Sheet (`components/artifact/artifact-reader.tsx`), which inbox, projects and ds
 *   also use. Do not import this pane outside the task page.
 *
 * ## The controls (two rows, two owners)
 *   · the pane's BAND (pinned, `shrink-0`, OUTSIDE the scroller): ✕ close · ⤢ full screen ·
 *     ⬇ download. Pinned because the scroller can pan HORIZONTALLY under wide content (a
 *     board, a table) - controls inside it rode the pan and clipped off-window:
 *     first the close, then Download. A control that can scroll away is
 *     not a control. The download link comes from ArtifactView's ONE fetch
 *     (`onDownloadInfo`), always for the version being viewed; the title row's copy is
 *     suppressed (`showDownload={false}`).
 *   · ArtifactView's title row (inside the content): version picker · verdict · meta.
 *
 * ## Behaviors owned here
 *   · width - **the row is a GRID and grid arbitrates** (`splitColumns`):
 *     `minmax(480px, 1fr) minmax(400px, <pct>%)`. The flex predecessor let the pane's
 *     %-width and the chat's min-width fight independently - the chat lost, crushed to
 *     zero while the pane painted past its ceiling. Grid resolves both tracks together;
 *     overflow is structurally impossible. The share persists (`STORAGE.splitPct`, own
 *     key - never the overlay drawer's); defaults are COMPACT (40% prose / 52%
 *     board-grid) so the chat keeps the maximum area. Drag the left edge (arrow keys
 *     too; double-click resets) - mid-drag writes the template straight onto the row
 *     (zero transcript re-renders) and commits the settled share to the route.
 *   · Escape closes the pane (unless something already handled the key)
 *   The pane owns NO artifact state - `artifactId` in, `onClose` out; the route owns the
 *   pct state, the grid template, auto-open on publish, and the dismissal latch.
 */

import { Download, Maximize2, X } from "lucide-react";
import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { Link } from "react-router";
import { ArtifactView } from "~/components/artifact/artifact-view";
import { VerdictBadge } from "~/components/kit";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import type { components } from "~/lib/api/types.gen";
import { STORAGE } from "~/lib/storage";
import type { ArtifactView as ShelfArtifact } from "~/lib/task-view";

type DownloadOut = components["schemas"]["DownloadOut"];

const PCT_KEY = STORAGE.splitPct; // the split's OWN preference - never the overlay's
const MIN_PX = 400;
/** The conversation's FLOOR - the grid's first minmax track; the pane may never crush the
 *  chat below it. */
const CHAT_MIN_PX = 480;
// Kinds whose natural shape is a board/grid, not a reading column - their DEFAULT is wider.
const WIDE_KINDS = new Set(["collection", "spreadsheet", "dataset"]);
// The pane's share of the row, by kind - the chat keeps the rest (the maximum area).
const defaultPct = (kind?: string | null) => (WIDE_KINDS.has(kind ?? "") ? 52 : 40);
const clampPct = (p: number) => Math.min(85, Math.max(15, p));

/** The artifact-mode row's grid template - THE arbitration. The flex
 *  predecessor gave the pane a %-clamp width and the chat a min-width and let them fight:
 *  flex negotiates the two independently, so a 70% pane width flatly won, the chat crushed
 *  toward zero, and the pane painted past its ceiling (seen live in devtools). Grid's
 *  minmax tracks are resolved TOGETHER by the layout algorithm - the chat's floor and the
 *  pane's share cannot both be violated, and overflow is structurally impossible. */
export const splitColumns = (pct: number) =>
  `minmax(${CHAT_MIN_PX}px, 1fr) minmax(${MIN_PX}px, ${clampPct(pct)}%)`;

/** The stored share, or the kind's default - route owns the state (it renders the grid);
 *  this module owns the math. Storage-guarded: the route computes this on every render,
 *  including ones no browser is running. */
export const readSplitPct = (kindHint?: string | null) => {
  if (typeof localStorage === "undefined") return defaultPct(kindHint);
  const saved = Number(localStorage.getItem(PCT_KEY));
  return saved > 0 ? clampPct(saved) : defaultPct(kindHint);
};

export function ReaderPane({
  artifactId,
  kindHint,
  title,
  verdict,
  pct,
  onPctCommit,
  onClose,
  onArtifactChanged,
  readOnly = false,
}: {
  artifactId: string;
  /** The artifact's wire type when the opener knows it - wide kinds open at a wider default. */
  kindHint?: string | null;
  /** Band identity, from the route's live shelf - the verdict updates the moment the
   *  receipt frame lands, no refetch. */
  title?: string;
  verdict?: ShelfArtifact["verdict"];
  /** The pane's current share of the row - route renders it into the grid template. */
  pct: number;
  /** A settled drag/keyboard/reset - route persists and re-renders the template. */
  onPctCommit: (pct: number) => void;
  onClose: () => void;
  /** A save/restore minted a new head - the route repoints `openArtifact` so the
   *  band's title/verdict keep tracking the shelf entry the reducer just re-keyed. */
  onArtifactChanged?: (artifactId: string) => void;
  /** A viewer reads, never writes: no edit, restore or label in the pane. */
  readOnly?: boolean;
}) {
  const sectionRef = useRef<HTMLElement>(null);

  // **CSS owns responsiveness; JS only translates a drag.** The row is a GRID
  // (`splitColumns`); during a drag we write the template straight onto the row element -
  // one style write per frame, zero React renders of the transcript - and commit the
  // settled share upward on release. Window resizes, sidebar drags, sidebar collapse: the
  // grid re-solves both tracks continuously, no listeners, no measuring.
  const pctRef = useRef(pct);
  pctRef.current = pct;

  // The Download link, reported by ArtifactView's one fetch - always for the version being
  // VIEWED. It renders in the pinned band; the title row's copy is suppressed (it rode the
  // horizontal pan of wide content and clipped off-window).
  const [download, setDownload] = useState<DownloadOut | null>(null);
  useEffect(() => setDownload(null), [artifactId]);

  // Escape closes - the split has no overlay to click away on.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.defaultPrevented) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const row = () => sectionRef.current?.parentElement;
  const commit = (p: number) => {
    pctRef.current = clampPct(Math.round(p * 10) / 10);
    onPctCommit(pctRef.current);
  };
  /** Pointer → the pane's share of the row. The one measurement left, and only during
   *  a drag: everything else is the grid template. */
  const pctFromPointer = (clientX: number) => {
    const host = row();
    if (!host) return pctRef.current;
    const rect = host.getBoundingClientRect();
    return clampPct(((rect.right - clientX) / rect.width) * 100);
  };
  const beginResize = (e: ReactPointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";
  };
  const resize = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
    // Straight onto the row, no React in the loop - the transcript never re-renders
    // mid-drag; the committed pct re-renders the same template once, on release.
    const p = pctFromPointer(e.clientX);
    pctRef.current = p;
    const host = row();
    if (host) host.style.gridTemplateColumns = splitColumns(p);
  };
  const endResize = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
    e.currentTarget.releasePointerCapture(e.pointerId);
    document.body.style.userSelect = "";
    document.body.style.cursor = "";
    commit(pctRef.current);
  };

  return (
    <section
      ref={sectionRef}
      aria-label="Artifact reader"
      className="bg-background relative flex h-full min-h-0 min-w-0 flex-col overflow-hidden border-l"
    >
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize artifact pane"
        aria-valuenow={Math.round(pct)}
        aria-valuemin={15}
        aria-valuemax={85}
        tabIndex={0}
        onPointerDown={beginResize}
        onPointerMove={resize}
        onPointerUp={endResize}
        onPointerCancel={endResize}
        onDoubleClick={() => commit(defaultPct(kindHint))}
        onKeyDown={(e) => {
          if (e.key === "ArrowLeft") commit(pctRef.current + 2);
          if (e.key === "ArrowRight") commit(pctRef.current - 2);
        }}
        className="hover:bg-accent focus-visible:ring-ring absolute inset-y-0 left-0 z-10 w-1.5 cursor-col-resize touch-none outline-none select-none focus-visible:ring-2"
      />
      {/* The pane's band - PINNED (shrink-0, outside the scroller): the reference's
          top-left cluster. The scroller below can pan horizontally under wide content;
          these two must never ride along. */}
      {/* h-14, exactly the chat band's height: the two headers share ONE horizontal line
          across the split - a mismatched rule under the headers reads as breakage. */}
      <div className="flex h-14 shrink-0 items-center gap-0.5 border-b px-1.5">
        <IconButton
          variant="ghost"
          size="sm"
          icon={<Icon icon={X} />}
          aria-label="Close"
          onClick={onClose}
        />
        <IconButton
          variant="ghost"
          size="sm"
          icon={<Icon icon={Maximize2} />}
          aria-label="Full screen"
          nativeButton={false}
          render={<Link to={`/artifacts/${artifactId}`} />}
        />
        {download && (
          <IconButton
            variant="ghost"
            size="sm"
            icon={<Icon icon={Download} />}
            aria-label="Download"
            nativeButton={false}
            render={<a href={download.url} download={download.filename ?? undefined} />}
          />
        )}
        {/* The artifact's identity lives HERE now, not in a PageHeader block inside the
            scroller - that block's height pushed an app-frame board past the pane and
            minted a scroll-inside-a-scroll. Verdict rides the live shelf: the badge flips
            the moment the receipt frame lands. */}
        {title && (
          <span
            className="min-w-0 flex-1 truncate pl-1.5 text-body-sm-regular font-medium"
            title={title}
          >
            {title}
          </span>
        )}
        {title && <VerdictBadge verdict={verdict} className="shrink-0" />}
      </div>
      {/* ONE scroll - this host's, for every kind (collections left the
          app-frame contract; a board bounded to the frame scrolled inside itself, and the
          user named it ugly twice - a collection now flows at full height like a document,
          panning wide layouts horizontally inside its own ViewShell box).
          overflow-x stays hidden: the pane never scrolls sideways. */}
      <div className="scrollbar-thin min-h-0 min-w-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-contain px-4 pt-3 pb-6">
        <ArtifactView
          artifactId={artifactId}
          chrome="bare"
          showDownload={false}
          onDownloadInfo={setDownload}
          onArtifactChanged={onArtifactChanged}
          readOnly={readOnly}
        />
      </div>
    </section>
  );
}
