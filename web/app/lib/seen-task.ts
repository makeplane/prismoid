/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { TaskView } from "~/lib/task-view";

// The task page, and a task's card in a bot's chat, tell the server the viewer has seen the task:
// their own inbox items about it read (POST /v1/inbox/tasks/{id}/read). The page says so when it
// opens and again whenever the stream brings news that could have knocked their inbox; a card
// only on news, so opening a long chat is not one call per card. Those knocks are born with a
// turn settling (delivered, failed, a budget stop) or a gate opening, so the turn, its status and
// the open gates are the news; which event knocked is the server's to know, not the page's. A
// page in a hidden tab has seen nothing: it waits until it is shown. A replay walks the record's
// past statuses in a burst, so the page reports once the view has been still for SEEN_SETTLE_MS.
// Read before the phone's alert goes out (it waits a few seconds), the item rings nothing there.

export const SEEN_SETTLE_MS = 400;

/** The part of the stream's view the page reports on: the turn, its status, the open gates' ids. */
export type SeenView = Pick<TaskView, "status" | "turn"> & {
  gates: readonly { gate_id: string }[];
};

/** What the page has shown that could have knocked the viewer's inbox. A new value is news. */
export function seenKey(view: SeenView): string {
  const open = view.gates.map((g) => g.gate_id).sort();
  return `${view.turn}|${view.status}|${open.join(",")}`;
}

/** Mark the task seen now: the page is shown and has news it has not reported yet. */
export function shouldMarkSeen(key: string, reported: string | null, visible: boolean): boolean {
  return visible && key !== reported;
}

/** Whether the sidebar's badge needs a re-read after a mark: the call read something, or the
 *  count the server now holds differs from the badge (a gate answered here read its own knocks
 *  server-side, so the call itself had nothing left to read). */
export function badgeStale(
  read: { marked: number; unread: number },
  badge: number | undefined,
): boolean {
  return read.marked > 0 || (badge !== undefined && read.unread !== badge);
}

/** What a task's card in a bot's chat has shown that could have knocked the viewer's inbox: its
 *  newest row (a turn started, a gate asked, how a turn settled) or the bot's line a continued
 *  turn came home as. A new value is news. */
export function cardSeenKey(rows: readonly { seq: number }[], botSeq: number | undefined): string {
  return String(rows.reduce((newest, r) => Math.max(newest, r.seq), botSeq ?? 0));
}
