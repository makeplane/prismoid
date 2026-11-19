/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The task stream's stall watchdog, pure (hooks/use-task-stream.ts wires it to the socket).

A live task that goes quiet past STALL_MS surfaces "waiting for signal…" so the page never dies
quietly over a dead worker. What counts as signal is the load-bearing part: presence
is who is WATCHING, not whether the work moves. Every open stream hears its own
`presence.here` on each reauth wake (10 s, under STALL_MS), so a watchdog that counted presence
could never fire at all.
*/

import { TERMINAL, type TaskStatus } from "~/lib/api/events";
import type { FeedItem } from "~/lib/task-view";

/** Running-silence past this surfaces the "waiting for signal…" hint. */
export const STALL_MS = 15_000;

/** Is this SSE event name presence (who is watching)? */
export function isPresenceType(type: string): boolean {
  return type.startsWith("presence.");
}

/** Does a frame with this SSE event name prove the TASK is alive? Presence never does, and
 *  neither does `screen.*`: the live screen's lease and action boxes are viewer-driven
 *  state that arrives while the work is stuck - a person holding the browser must not mask a
 *  stalled worker. */
export function provesLife(type: string): boolean {
  return !isPresenceType(type) && !type.startsWith("screen.");
}

/** The last-signal clock after a frame arrives at `now`: presence leaves it where it was. */
export function heard(lastSignalAt: number, type: string, now: number): number {
  return provesLife(type) ? now : lastSignalAt;
}

/** The watchdog's verdict: a task that is not settled, silent past `limitMs`, is stalled. */
export function isStalled(
  status: TaskStatus,
  lastSignalAt: number,
  now: number,
  limitMs: number = STALL_MS,
): boolean {
  return !TERMINAL.has(status) && now - lastSignalAt > limitMs;
}

/** How long silence may last while the step waits on its Machine, as the SERVER states it: the
 *  `wait_s` bound its Machine phase carried (the worker's wait gives up after it and says nothing
 *  in between), in ms, from the live turn's pending `machine` row. Null when no such row stands
 *  or it carries no bound (an older event): the plain STALL_MS rule applies, with no copy of any
 *  server number here. */
export function systemWaitLimitMs(feed: readonly FeedItem[]): number | null {
  for (let i = feed.length - 1; i >= 0; i--) {
    const item = feed[i];
    if (item.kind !== "execution") continue;
    if (item.settled) return null;
    const waitS = pendingMachineWaitS(item);
    return waitS ? waitS * 1000 : null;
  }
  return null;
}

/** The bound the turn's pending Machine row carries, in seconds, if one stands. */
function pendingMachineWaitS(item: Extract<FeedItem, { kind: "execution" }>): number | null {
  for (const g of item.groups)
    for (const r of g.rows)
      if (r.t === "tool" && r.tool === "machine" && r.pending && r.waitS) return r.waitS;
  return null;
}
