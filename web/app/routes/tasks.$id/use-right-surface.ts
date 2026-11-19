/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { screenPaneOpens } from "~/lib/machine-copy";
import { STORAGE } from "~/lib/storage";
import type { ArtifactView } from "~/lib/task-view";
import { readSplitPct, splitColumns } from "./reader-pane";
import { latestPublish, opensOnPublish } from "./right-surface";
import { SCREEN_PCT } from "./screen-pane";
import { useWideViewport } from "./task-rail";

const RAIL_KEY = STORAGE.taskRail;

// Right-side changes must read as a HORIZONTAL move. Mount/unmount reflows the column, the
// browser keeps the old scrollTop, and the tail-follow re-pins a frame later - a visible
// vertical settle (the "diagonal" motion). So: note whether the transcript was at its tail,
// commit synchronously (flushSync reflows before paint), and re-pin in the same frame - the
// content only slides sideways.
function withTailPin(mutate: () => void) {
  const vp = document.querySelector<HTMLElement>('[data-slot="message-scroller-viewport"]');
  const pinned = vp != null && vp.scrollHeight - vp.scrollTop - vp.clientHeight < 48;
  flushSync(mutate);
  if (pinned && vp) vp.scrollTop = vp.scrollHeight;
}

/**
 * THE RIGHT SIDE - one surface at a time (the page's law). Two surfaces may own the right
 * edge, never both:
 *   · the RAIL   - a floating card (Progress · Outputs · Sources · Usage), pref-persisted
 *   · the READER - a true split pane that PUSHES the transcript (reader-pane.tsx)
 * Opening the reader hides the rail (its content stays one click away as a centered MODAL
 * behind the band's Progress button); closing the reader restores the rail - the preference
 * never moved. Narrow screens keep the overlay Sheet pair instead. The screen takes the
 * reader's split slot: one or the other, never both. It opens only on a task that HAS a
 * Machine, and never on a device Machine (its browser is a window on the person's own
 * computer, with no screen to relay).
 */
export function useRightSurface({
  machine,
  artifacts,
  allArtifacts,
  live,
  superseded,
}: {
  machine: { driver?: string | null } | null | undefined;
  /** This task's own artifacts: a publish among them may open the reader. */
  artifacts: ArtifactView[];
  /** Every artifact the page can open, the thread's earlier turns included. */
  allArtifacts: ArtifactView[];
  live: boolean;
  superseded: Readonly<Record<string, string>>;
}) {
  const [openArtifact, setOpenArtifact] = useState<string | null>(null);
  const [openScreenWanted, setOpenScreen] = useState(false);
  const screenOpen = screenPaneOpens(openScreenWanted, machine);
  const wide = useWideViewport();
  const [railPref, setRailPref] = useState(
    () => typeof window === "undefined" || localStorage.getItem(RAIL_KEY) !== "closed",
  );
  const [railSheet, setRailSheet] = useState(false);
  const dismissed = useRef<string | null>(null);
  // ARTIFACT MODE (wide + the split open: the reader, or the screen): the pane takes its own
  // full-height plane and the rail escalates to a centered modal behind the band's Progress
  // button. The screen wears the same mode - one split slot, one layout.
  const artifactMode = wide && (openArtifact != null || screenOpen);
  const [railModal, setRailModal] = useState(false);
  // The split's share of the row (reader-pane.tsx owns the math; the page owns the state
  // because the page renders the grid template the two panes live in).
  const [splitPct, setSplitPct] = useState<number | null>(null);
  const commitSplitPct = useCallback((p: number) => {
    setSplitPct(p);
    localStorage.setItem(STORAGE.splitPct, String(p));
  }, []);

  const toggleRail = () => {
    withTailPin(() => {
      if (openArtifact || screenOpen) {
        // The topbar toggle while reading = "give me the rail back": one gesture closes
        // the pane and shows the rail, honoring the one-surface law in the user's favor.
        if (openArtifact) dismissed.current = openArtifact;
        setRailModal(false);
        setOpenArtifact(null);
        setOpenScreen(false);
        if (wide) {
          setRailPref(true);
          localStorage.setItem(RAIL_KEY, "open");
        } else {
          setRailSheet(true);
        }
        return;
      }
      if (wide) {
        setRailPref((p) => {
          localStorage.setItem(RAIL_KEY, p ? "closed" : "open");
          return !p;
        });
      } else {
        setRailSheet((s) => !s);
      }
    });
  };
  // Opening an artifact closes the screen (one split slot). Stable: memoized rows hold it.
  const showArtifact = useCallback((id: string) => {
    setOpenScreen(false);
    setOpenArtifact(id);
  }, []);
  // Stable so the memoized TaskRail skips re-rendering on every streamed delta.
  const openArtifactFromRail = useCallback(
    (id: string) => {
      showArtifact(id);
      setRailSheet(false); // on narrow screens the reader replaces the rail overlay
    },
    [showArtifact],
  );
  // Opening the screen closes the reader, WITHOUT the dismissal latch: this is "show me the
  // Machine", not "stop showing me that artifact". On narrow screens it replaces the rail sheet.
  const openScreenPane = useCallback(() => {
    setOpenArtifact(null);
    setRailSheet(false);
    setRailModal(false);
    setOpenScreen(true);
  }, []);
  const closeScreen = () => withTailPin(() => setOpenScreen(false));
  const closeReader = () => {
    // The dismissal latch: a manual close means "stop showing me THIS artifact" - the
    // auto-open below respects it until a different artifact (or new revision) publishes.
    dismissed.current = openArtifact;
    setRailModal(false); // a modal left true would pop uninvited on the NEXT artifact
    withTailPin(() => setOpenArtifact(null));
  };

  // A publish surfaces the artifact IN PLACE (right-surface.ts). This is also where the
  // streaming draft LANDS: the draft card retires at publish, and the open reader is its
  // continuity - without it the text the person was reading simply vanishes into a compact
  // card. The first publish opens too: it is every normal run's publish moment, and the live
  // check already protects settled replays. The latch respects a deliberate close.
  const seenArtifact = useRef<string>("");
  useEffect(() => {
    const latest = latestPublish(artifacts);
    if (!latest || latest.key === seenArtifact.current) return;
    seenArtifact.current = latest.key;
    if (
      opensOnPublish(latest.id, {
        live,
        dismissed: dismissed.current,
        superseded,
        screenOpen,
      })
    ) {
      setOpenArtifact(latest.id);
    }
  }, [artifacts, live, superseded, screenOpen]);

  const openEntry = openArtifact ? allArtifacts.find((a) => a.id === openArtifact) : undefined;
  const openKind = openEntry?.type;
  const panePct = splitPct ?? readSplitPct(openKind);

  // The grid template is written IMPERATIVELY, never as a React style prop: mid-drag the
  // pane writes the template straight onto this row per pointer frame, and a streamed frame
  // re-rendering the page would have React re-apply the STALE state value against the
  // pointer - a visible snap-back on any live task. The effect writes only when the COMMITTED
  // share (or the mode) changes; streamed re-renders leave the row alone.
  const rowRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const row = rowRef.current;
    if (!row) return;
    if (artifactMode)
      row.style.gridTemplateColumns = splitColumns(screenOpen ? SCREEN_PCT : panePct);
    else row.style.removeProperty("grid-template-columns");
  }, [artifactMode, screenOpen, panePct]);

  return {
    wide,
    artifactMode,
    rowRef,
    // the reader
    openArtifact,
    setOpenArtifact,
    openEntry,
    openKind,
    panePct,
    commitSplitPct,
    showArtifact,
    closeReader,
    // the screen
    screenOpen,
    setOpenScreen,
    openScreenPane,
    closeScreen,
    // the rail
    railPref,
    railSheet,
    setRailSheet,
    railModal,
    setRailModal,
    toggleRail,
    openArtifactFromRail,
  };
}

export type RightSurface = ReturnType<typeof useRightSurface>;
