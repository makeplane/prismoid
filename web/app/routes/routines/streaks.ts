/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Quiet runs collapse into streaks ("quiet runs collapse into a streak line in
// the ledger - the axis never becomes notification spam", applied to its own screen). A run
// is QUIET when the gate stood down on purpose: skipped_* and superseded. Mints, holds, loop
// suppressions, and errors are each their own news and never collapse.

import type { components } from "~/lib/api/types.gen";

export type RunRow = components["schemas"]["RoutineRunOut"];

/** A stored decision as the ledger says it: its own words, spaced ("skipped overlap"). */
export function decisionLabel(decision: string): string {
  return decision.replace(/_/g, " ");
}

const QUIET = new Set([
  "skipped_overlap",
  "skipped_missed",
  "skipped_throttle",
  "skipped_budget",
  "skipped_cap",
  "skipped_season",
  "skipped_membership",
  "skipped_allowance",
  "superseded",
]);

export type LedgerItem =
  | { kind: "run"; run: RunRow }
  | { kind: "streak"; count: number; latest: RunRow; decisions: string[] };

/** Rows arrive newest-first; consecutive quiet rows fold into one streak keyed by its latest. */
export function collapseRuns(rows: RunRow[]): LedgerItem[] {
  const out: LedgerItem[] = [];
  for (const run of rows) {
    if (!QUIET.has(run.decision)) {
      out.push({ kind: "run", run });
      continue;
    }
    const tail = out[out.length - 1];
    if (tail?.kind === "streak") {
      tail.count += 1;
      if (!tail.decisions.includes(run.decision)) tail.decisions.push(run.decision);
    } else {
      out.push({ kind: "streak", count: 1, latest: run, decisions: [run.decision] });
    }
  }
  return out;
}
