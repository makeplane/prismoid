/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { DAY_MS, dayIndex, mondayOnOrBefore, sundayOnOrAfter } from "../core/date-math";
import {
  AXIS_CAP_DAYS,
  EDGE_PX,
  EXTEND_DAYS,
  ZOOMS,
  autoZoom,
  extendAxis,
  fitAxis,
  initialAxis,
  rezoomScrollLeft,
  type Axis,
  type Zoom,
} from "./axis";

// renderToStaticMarkup never runs effects and SSR warns on useLayoutEffect - same math, no DOM.
const useIsomorphicLayoutEffect = typeof window === "undefined" ? useEffect : useLayoutEffect;

type Pending =
  | { kind: "delta"; px: number } // prepend compensation: widen, then shift scrollLeft in-frame
  | { kind: "setLeft"; px: number } // zoom: land on the center-preserving position pre-paint
  | { kind: "smoothCenter"; ms: number }; // today outside the axis: extend, then glide to it

/**
 * The windowed-infinite timeline's state machine. The SHELL owns the scroll element; this hook
 * consumes its ref - measuring it, listening to it, and writing compensation scrolls into it.
 *
 * The one hard rule (why `pendingRef` exists): when the axis PREPENDS days, the canvas widens
 * at the left, so everything the user sees would shift right by `deltaDays·dayPx`. The state
 * update and the `scrollLeft +=` write must land in the same frame - React commits the wider
 * DOM, the layout effect below adds the delta BEFORE paint, and the viewport-relative content
 * position is identical at next paint. No flushSync (a sync render per scroll event is jank),
 * no scroll-behavior:smooth on the scroller (it would animate the compensation into a lurch).
 */
export function useGanttViewport(opts: {
  scrollerRef: RefObject<HTMLDivElement | null>;
  dataMinMs: number | null;
  dataMaxMs: number | null;
  /** TEST SEAM: seeds containerW so the fit math is exercisable under static render. */
  initialContainerWidth?: number;
}) {
  const { scrollerRef, dataMinMs, dataMaxMs } = opts;

  const [state, setState] = useState(() => {
    const min = dataMinMs ?? Date.parse(new Date().toISOString().slice(0, 10) + "T00:00:00Z");
    const max = dataMaxMs ?? min;
    const paddedDays =
      dayIndex(sundayOnOrAfter(max + 3 * DAY_MS), mondayOnOrBefore(min - 3 * DAY_MS)) + 1;
    const zoom = autoZoom(paddedDays);
    const containerW = opts.initialContainerWidth ?? 0;
    return { zoom, containerW, axis: initialAxis(min, max, ZOOMS[zoom].dayPx, containerW) };
  });

  const pendingRef = useRef<Pending | null>(null);
  const extendLockRef = useRef(false);
  const dayPx = ZOOMS[state.zoom].dayPx;

  // Mount + resize: measure the scrollport; growth re-fits (END-extend only - shrinking would
  // teleport the scroll position under the user). A PASSIVE effect on purpose: the scroller is
  // an ANCESTOR (the shell's div), and on the first shared commit React attaches an ancestor's
  // ref only AFTER descendant layout effects have run - a layout-effect measure here reads
  // null, bails, and with empty deps never retries, leaving the canvas at its unfit width
  // until a view switch remounts us. Passive effects run after every ref is attached; the fit
  // only ever appends to the right, so the one-frame unfit paint cannot visibly jump.
  useEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const measure = () => {
      const w = el.clientWidth;
      setState((s) =>
        w === s.containerW
          ? s
          : { ...s, containerW: w, axis: fitAxis(s.axis, ZOOMS[s.zoom].dayPx, w) },
      );
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [scrollerRef]);

  // Edge extension. Passive native listener on the shell scroller; the lock holds off
  // momentum-scroll double-fires between the triggering event and the commit below.
  const onScrollRef = useRef<() => void>(() => {});
  onScrollRef.current = () => {
    const el = scrollerRef.current;
    if (!el || extendLockRef.current) return;
    const left = el.scrollLeft;
    if (left < EDGE_PX) {
      const { axis, deltaDays } = extendAxis(state.axis, "start", EXTEND_DAYS, AXIS_CAP_DAYS);
      if (deltaDays === 0) return; // at the cap: an honest hard edge
      extendLockRef.current = true;
      pendingRef.current = { kind: "delta", px: deltaDays * dayPx };
      setState((s) => ({ ...s, axis }));
    } else if (left + el.clientWidth > el.scrollWidth - EDGE_PX) {
      const { axis, deltaDays } = extendAxis(state.axis, "end", EXTEND_DAYS, AXIS_CAP_DAYS);
      if (deltaDays === 0) return;
      extendLockRef.current = true;
      pendingRef.current = null; // appending shifts nothing
      setState((s) => ({ ...s, axis }));
    }
  };
  useEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const handler = () => onScrollRef.current();
    el.addEventListener("scroll", handler, { passive: true });
    return () => el.removeEventListener("scroll", handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- attach once: the handler reads the latest scroll logic through onScrollRef
  }, []);

  // Data-edit sync: a peek edit can move a date outside the axis; the axis only ever GROWS.
  useEffect(() => {
    if (dataMinMs == null || dataMaxMs == null) return;
    setState((s) => {
      let { startMs, endMs } = s.axis;
      let deltaDays = 0;
      if (dataMinMs < startMs) {
        const nextStart = mondayOnOrBefore(dataMinMs - 3 * DAY_MS);
        deltaDays = dayIndex(startMs, nextStart);
        startMs = nextStart;
      }
      if (dataMaxMs > endMs) endMs = sundayOnOrAfter(dataMaxMs + 3 * DAY_MS);
      if (startMs === s.axis.startMs && endMs === s.axis.endMs) return s;
      if (deltaDays > 0)
        pendingRef.current = { kind: "delta", px: deltaDays * ZOOMS[s.zoom].dayPx };
      return { ...s, axis: { startMs, endMs } };
    });
  }, [dataMinMs, dataMaxMs]);

  // THE compensation commit: after React mutates the DOM (canvas now wider), before paint.
  // Direct property write - immune to CSS scroll-behavior; the compensating scroll event
  // re-enters the handler with both edge conditions false, so it is naturally idempotent.
  useIsomorphicLayoutEffect(() => {
    const el = scrollerRef.current;
    const pending = pendingRef.current;
    pendingRef.current = null;
    extendLockRef.current = false;
    if (!el || !pending) return;
    if (pending.kind === "delta") {
      el.scrollLeft += pending.px;
    } else if (pending.kind === "setLeft") {
      el.scrollLeft = pending.px;
    } else {
      const target = (dayIndex(pending.ms, state.axis.startMs) + 0.5) * dayPx - el.clientWidth / 2;
      el.scrollTo({
        left: Math.max(0, Math.min(target, el.scrollWidth - el.clientWidth)),
        behavior: "smooth",
      });
    }
  }, [state.axis.startMs, state.axis.endMs, state.zoom]);

  const setZoom = (zoom: Zoom) => {
    if (zoom === state.zoom) return;
    const el = scrollerRef.current;
    if (el) {
      pendingRef.current = {
        kind: "setLeft",
        px: rezoomScrollLeft(
          el.scrollLeft,
          el.clientWidth,
          ZOOMS[state.zoom].dayPx,
          ZOOMS[zoom].dayPx,
        ),
      };
    }
    setState((s) => ({ ...s, zoom, axis: fitAxis(s.axis, ZOOMS[zoom].dayPx, s.containerW) }));
  };

  const todayMs = Date.parse(new Date().toISOString().slice(0, 10) + "T00:00:00Z");
  const scrollToToday = () => {
    const el = scrollerRef.current;
    if (!el) return;
    const { startMs, endMs } = state.axis;
    if (todayMs < startMs || todayMs > endMs) {
      pendingRef.current = { kind: "smoothCenter", ms: todayMs };
      setState((s) => ({
        ...s,
        axis: {
          startMs: todayMs < startMs ? mondayOnOrBefore(todayMs - 7 * DAY_MS) : startMs,
          endMs: todayMs > endMs ? sundayOnOrAfter(todayMs + 7 * DAY_MS) : endMs,
        },
      }));
      return;
    }
    const target = (dayIndex(todayMs, startMs) + 0.5) * dayPx - el.clientWidth / 2;
    el.scrollTo({
      left: Math.max(0, Math.min(target, el.scrollWidth - el.clientWidth)),
      behavior: "smooth",
    });
  };

  return {
    axis: state.axis as Axis,
    zoom: state.zoom,
    dayPx,
    setZoom,
    scrollToToday,
    todayMs,
  };
}
