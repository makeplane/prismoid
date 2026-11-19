/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** Worker liveness from /healthz/full - the only honest "is the machine on?" signal.

A queued task with a stopped worker looks identical to a busy queue on the task row alone.
The worker pulses `system_beats`; this module reads that age through /healthz/full (the vitals
moved behind auth when the box went public - bare /healthz says only ok/degraded; the typed
client carries the bearer) and maps it for the UI. Shared so the task page and any other
surface stay in one vocabulary.
*/

import { useEffect, useState } from "react";
import { api } from "~/lib/api/client";

/** Probe interval - first sample also waits this long so a brand-new task says "queued" briefly. */
export const LIVENESS_PROBE_MS = 15_000;

export type WorkerLiveness = "unknown" | "ok" | "offline" | "unreachable";

/** Map the healthz worker check string into a coarse UI state. */
export function interpretWorkerCheck(worker: string | undefined): WorkerLiveness {
  if (worker === "ok") return "ok";
  // "unknown" means the beat QUERY failed (a transient read), not that the worker is gone -
  // claim only what the probe actually knows. Offline is reserved for never/stale.
  if (worker === undefined || worker === "unknown") return "unknown";
  return "offline";
}

export async function probeWorkerLiveness(): Promise<WorkerLiveness> {
  try {
    const { data, error } = await api.GET("/healthz/full");
    if (error || !data) return "unreachable";
    const worker = data.checks?.worker;
    return interpretWorkerCheck(worker);
  } catch {
    return "unreachable";
  }
}

/**
 * Poll /healthz for worker liveness.
 * @param enabled - set false when the surface no longer needs the probe (e.g. task left created)
 * @param immediate - probe on mount (default false: first 15 s stay "unknown" / calmer claim)
 */
export function useWorkerLiveness(enabled = true, immediate = false): WorkerLiveness {
  const [state, setState] = useState<WorkerLiveness>("unknown");

  useEffect(() => {
    if (!enabled) {
      setState("unknown");
      return;
    }
    let live = true;
    const run = async () => {
      const next = await probeWorkerLiveness();
      if (live) setState(next);
    };
    if (immediate) void run();
    const every = window.setInterval(() => void run(), LIVENESS_PROBE_MS);
    return () => {
      live = false;
      window.clearInterval(every);
    };
  }, [enabled, immediate]);

  return state;
}
