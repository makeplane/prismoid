/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** Silky streaming: deltas arrive in lumps; the reader should see a steady pour.

The stream stays authoritative - this only paces the REVEAL of text that has already
arrived. A rAF loop releases characters at a rate proportional to the backlog, so it drips
when caught up and sprints when behind; the moment a segment settles it snaps to the full
text. Pacing also caps markdown re-parses at the frame budget instead of once per delta.

Reveal prefers word boundaries when the backlog is large enough, so we rarely cut mid-
`**bold**` or mid-link and flash raw markdown. Small backlogs still drip character-wise
so the last few tokens don't stall waiting for a project.
*/

import { useEffect, useRef, useState } from "react";

/** Characters to release this frame: ~1/12 of the backlog, at least one. A constant fraction
 *  gives an exponential glide - big bursts land fast, the tail eases out. */
export function revealStep(shownLength: number, targetLength: number): number {
  const backlog = targetLength - shownLength;
  if (backlog <= 0) return 0;
  return Math.max(1, Math.ceil(backlog / 12));
}

/**
 * Advance `from` toward `target` by ~`step` characters, preferring a word boundary
 * in the look-ahead window so markdown mid-token flicker is rare.
 */
export function advanceReveal(target: string, from: number, step: number): number {
  if (step <= 0 || from >= target.length) return from;
  const ideal = Math.min(target.length, from + step);
  if (ideal >= target.length) return target.length;
  // Small steps: character drip (last tokens shouldn't wait for whitespace).
  if (step < 4) return ideal;
  // Look a little past the ideal for a natural break (space, newline, punctuation).
  const windowEnd = Math.min(target.length, ideal + 12);
  const slice = target.slice(ideal, windowEnd);
  const breakAt = slice.search(/[\s\n.,;:!?*\])}`]/);
  if (breakAt >= 0) return ideal + breakAt + 1;
  // Look slightly before ideal for a break so we don't cut a long word mid-glyph run.
  const back = target.slice(from, ideal);
  const backBreak = back.search(/[\s\n](?=[^\s\n]*$)/);
  if (backBreak >= 0 && from + backBreak + 1 > from) {
    return from + backBreak + 1;
  }
  return ideal;
}

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** The paced reveal of `target` while `live`; the full string, instantly, once settled.
 *  If `target` stops extending what is shown (a durable rewrite normalised the text), snap -
 *  never animate a correction. Reduced motion always snaps. */
export function useSmoothText(target: string, live: boolean): string {
  const reduced = prefersReducedMotion();
  const animate = live && !reduced;
  const [shown, setShown] = useState(animate ? "" : target);
  const shownRef = useRef(shown);
  shownRef.current = shown;
  const targetRef = useRef(target);
  targetRef.current = target;

  useEffect(() => {
    if (!live || reduced) {
      shownRef.current = target;
      setShown(target);
      return;
    }
    if (!target.startsWith(shownRef.current)) {
      shownRef.current = target; // rewritten, not extended - snap
      setShown(target);
      return;
    }
    // Already caught up: do not arm a forever rAF; re-arm when target grows.
    if (shownRef.current.length >= target.length) {
      return;
    }

    let frame = 0;
    let last = 0;
    let cancelled = false;
    const tick = (now: number) => {
      if (cancelled) return;
      // ~30 fps: half the frame budget is plenty for prose and halves the markdown parses.
      if (now - last >= 32) {
        last = now;
        const full = targetRef.current;
        const from = shownRef.current.length;
        const step = revealStep(from, full.length);
        if (step > 0) {
          const nextLen = advanceReveal(full, from, step);
          const next = full.slice(0, nextLen);
          // Sync the ref immediately so a second tick before paint doesn't re-step the same base.
          shownRef.current = next;
          setShown(next);
        }
        // Caught up: stop the loop. Effect re-runs when target grows.
        if (shownRef.current.length >= full.length) {
          return;
        }
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => {
      cancelled = true;
      cancelAnimationFrame(frame);
    };
  }, [target, live, reduced]);

  return animate ? shown : target;
}
