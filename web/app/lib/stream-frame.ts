/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// What the task stream does with ONE frame, pure. hooks/use-task-stream.ts wires it to the
// EventSource; this is where the routing rule lives so it can be tested without a socket.
//
// The rule: presence is state about who is HERE. It folds into presence, never into
// the thread (the reducer never sees it), and it never counts as the task being alive (the
// stall watchdog's clock is only ever moved through stream-stall's `heard`). A frame is
// presence only if BOTH its SSE event name and its own `type` say so - a frame that disagrees
// with itself is dropped, so it can reach neither presence nor the reducer.

import type { TaskEvent } from "~/lib/api/events";
import { emptyPresence, foldPresence, isPresenceFrame, type PresenceState } from "~/lib/presence";
import { heard, isPresenceType, provesLife } from "~/lib/stream-stall";
import { reduce, type TaskView } from "~/lib/task-view";

export type FrameState = {
  view: TaskView;
  presence: PresenceState;
  /** The watchdog's clock: when the TASK last gave a sign of life (client ms). */
  lastSignalAt: number;
};

export type FrameResult = FrameState & {
  /** Did this frame prove the task alive (clear the "waiting for signal" hint)? */
  alive: boolean;
  /** Which half of the state moved; null = the frame changed nothing (dropped or ignored). */
  changed: "view" | "presence" | null;
};

function parse(data: string): TaskEvent | null {
  try {
    return JSON.parse(data) as TaskEvent;
  } catch {
    return null;
  }
}

/** Fold one SSE frame (`type` = the event name, `data` = its JSON body) received at `now`. */
export function foldFrame(state: FrameState, type: string, data: string, now: number): FrameResult {
  const lastSignalAt = heard(state.lastSignalAt, type, now);
  const alive = provesLife(type);
  const ignored = { ...state, lastSignalAt, alive, changed: null } as const;

  const ev = parse(data);
  if (!ev) return ignored; // a malformed frame must not tear the stream down

  const named = isPresenceType(type); // the SSE event name says presence
  const shaped = isPresenceFrame(ev); // and so does the body
  if (named || shaped) {
    if (!(named && shaped)) return ignored; // disagreeing with itself: neither presence nor thread
    try {
      return {
        ...state,
        presence: foldPresence(state.presence, ev, now),
        lastSignalAt,
        alive,
        changed: "presence",
      };
    } catch (err) {
      // A body that says presence but is missing what the fold reads (no actor, say) must
      // not throw at the socket: presence is a nicety, the stream is not.
      console.warn("dropped unfoldable presence frame", ev.type, err);
      return ignored;
    }
  }

  try {
    return { ...state, view: reduce(state.view, ev), lastSignalAt, alive, changed: "view" };
  } catch (err) {
    // One bad frame must not wedge the whole stream - skip it, keep folding.
    console.warn("dropped unfoldable frame", ev.type, err);
    return ignored;
  }
}

/** Fold the durable frames a tab already holds (lib/frame-log.ts) into a seed view, exactly as
 *  the server's replay of them would: same frames, same order, same reducer. */
export function foldKept(
  view: TaskView,
  frames: readonly { type: string; data: string }[],
): TaskView {
  let state: FrameState = { view, presence: emptyPresence(), lastSignalAt: 0 };
  for (const f of frames) state = foldFrame(state, f.type, f.data, 0);
  return state.view;
}
