/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// What an authoring job changes on the bot's rows when it lands: the drafter mints the next
// version, a selection-eval run writes the draft's verdict and report. The Configure tab re-reads
// the bot until this mark moves, so the result shows however long the worker takes.

type Row = { version: number; eval_status: string; eval_report?: unknown };

export function authoringMark(b: { newest: Row; draftAhead?: Row | null; head: Row }): string {
  const working = b.draftAhead ?? b.head;
  return JSON.stringify([
    b.newest.version,
    working.version,
    working.eval_status,
    working.eval_report,
  ]);
}

/** How long the tab keeps re-reading for a job whose mark never moves (a re-run that scored the
 *  same): long past any drafter or eval run, never forever. */
export const AUTHORING_CAP_MS = 180_000;
