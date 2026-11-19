/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useRef } from "react";
import { useRevalidator } from "react-router";
import { recheckScreens } from "~/hooks/use-screen";
import { ping } from "~/lib/api/client";
import type { components } from "~/lib/api/types.gen";
import { createBackgroundRefresh, type BackgroundRefresh } from "~/lib/background-refresh";
import { createReseeder, type Reseeder } from "~/lib/screen-reseed";
import { reseedScreen, type TaskView } from "~/lib/task-view";
import { chipBehindTheRun } from "./machine-chip";
import { stampsPending } from "./task-page";

type TaskDetailOut = components["schemas"]["TaskDetailOut"];

/**
 * The loader snapshot kept current by live events. The task read the page loaded with goes
 * stale in a few known ways, and each one asks for a re-read here:
 *   · the dial stamps (size, path, format) land at planning, after the snapshot - once per turn;
 *   · a `machine.*` frame moves the Machine chip's numbers;
 *   · a resumed step re-binds its parked Machine with no frame of its own;
 *   · a `screen.state` frame carries no order, so the lease is reseeded from a fresh read.
 * Every re-read goes through one background guard: a frame that lands while the API is down
 * (a restart publishes the lease's grace on its way out) must never swap this page for the
 * error screen. The stream coming back is how the page hears a server restart - the only way
 * the computer-use switch moves - so a screen that said "off" asks again on reconnect.
 */
export function useLiveRefresh({
  detail,
  view,
  connected,
  fleetThread,
  foldLocal,
}: {
  detail: TaskDetailOut;
  view: TaskView;
  connected: boolean;
  fleetThread: boolean;
  foldLocal: (fn: (v: TaskView) => TaskView) => void;
}) {
  const wasConnected = useRef(connected);
  useEffect(() => {
    if (connected && !wasConnected.current) recheckScreens();
    wasConnected.current = connected;
  }, [connected]);

  const revalidator = useRevalidator();
  const revalidatorRef = useRef(revalidator);
  revalidatorRef.current = revalidator;
  const background = useRef<BackgroundRefresh | null>(null);
  if (!background.current)
    background.current = createBackgroundRefresh({
      revalidate: () => void revalidatorRef.current.revalidate(),
      reachable: ping,
    });
  useEffect(() => () => background.current?.stop(), []);

  // The dial stamps: revalidate ONCE PER TURN so the header chips appear (and re-resolve) live
  // instead of on the next visit. Ref-latched per turn: a per-mount latch went stale after a
  // resizing follow-up.
  const askedForStamp = useRef(0); // the last turn a revalidate was asked for
  useEffect(() => {
    const pending = stampsPending(
      { status: view.status, turn: view.turn },
      {
        turn: detail.turn,
        scale: detail.scale,
        deliverable: detail.deliverable,
        deliverable_format: detail.deliverable_format,
      },
      fleetThread,
    );
    if (pending && askedForStamp.current < view.turn && revalidator.state === "idle") {
      askedForStamp.current = view.turn;
      background.current?.request();
    }
  }, [
    view.status,
    view.turn,
    detail.turn,
    detail.scale,
    detail.deliverable,
    detail.deliverable_format,
    fleetThread,
    revalidator,
  ]);

  // The chip's numbers come from the task detail: a machine.* event revalidates the loader.
  const machineSeen = useRef(view.machineVersion);
  useEffect(() => {
    if (view.machineVersion !== machineSeen.current) {
      machineSeen.current = view.machineVersion;
      background.current?.request();
    }
  }, [view.machineVersion]);

  // A resumed step re-binds its parked Machine with no machine.* frame (a pause, an answered
  // question): while the chip still says parked on a running task, each record frame re-reads
  // it once, and the first after the re-bind turns it.
  const chipBehind = chipBehindTheRun(detail.machine, view.status, view.gates.length);
  const chipCheckedAt = useRef(-1);
  useEffect(() => {
    if (!chipBehind || chipCheckedAt.current === view.appliedSeq) return;
    chipCheckedAt.current = view.appliedSeq;
    background.current?.request(); // a live frame's re-read waits for the API (background-refresh)
  }, [chipBehind, view.appliedSeq]);

  // `screen.state` frames carry no order, so a late one could leave a lease that is gone (or
  // hide one that is held): on each, re-read the task (debounced) and reseed `view.screen` from
  // `machine.control`. The web never expires a lease on its own clock.
  const reseeder = useRef<Reseeder | null>(null);
  if (!reseeder.current)
    reseeder.current = createReseeder({
      start: view.screenVersion,
      revalidate: () => background.current?.request(),
      state: () => (revalidatorRef.current.state === "loading" ? "loading" : "idle"),
      fold: (d: TaskDetailOut) => foldLocal((v) => reseedScreen(v, d)),
    });
  useEffect(() => reseeder.current?.version(view.screenVersion), [view.screenVersion]);
  useEffect(() => {
    reseeder.current?.revalidator(revalidator.state === "loading" ? "loading" : "idle", detail);
  }, [revalidator.state, detail]);
  useEffect(() => () => reseeder.current?.dispose(), []);

  return revalidator;
}
