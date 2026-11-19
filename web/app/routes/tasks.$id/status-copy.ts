/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** Shared human phrases for live task chrome - the pre-tree WorkingLine's voice.
 *  (The ticker vocabulary - BUSY/TICKER status sets, tickerPhase - left with
 *  live-ticker.tsx when the tail line became the one liveness home.) */

import type { StepView } from "~/lib/task-view";

// Durations live in ~/lib/time (formatSeconds/formatDurationMs - the one scale law);
// this file keeps the PHRASES.

/**
 * What the machine is doing - the transcript's bottom line.
 * Prefers the active step's title; falls back to phase verbs.
 */
export function workingPhrase(status: string, steps: StepView[]): string {
  if (status === "clarifying") return "deciding what to ask…";
  if (status === "planning") return "planning the approach…";
  if (status === "waiting") return "waiting for you…";
  if (status === "paused") return "paused";

  const active =
    steps.find((s) => s.state === "running") ??
    steps.find((s) => s.state === "verifying") ??
    steps.find((s) => s.state === "retrying");

  if (active) {
    const title = active.title?.trim();
    if (active.state === "verifying") {
      return title ? `verifying ${title}…` : "verifying…";
    }
    if (active.state === "retrying") {
      return title ? `retrying ${title}…` : "retrying…";
    }
    if (title) return `${title}…`;
  }

  return "working…";
}
