/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Replay a task's durable record (since=0, follow=false) and fold it - the same reducer the
// live stream uses, so a replayed transcript can never disagree with a streamed one.

import { API_URL, getToken } from "./api/client";
import { ALL_EVENT_TYPES, type TaskEvent } from "./api/events";
import { emptyView, reduce, type TaskView } from "./task-view";

// The reducer's exhaustive switch throws (assertNever) on a type it doesn't know. The LIVE
// path can't hit that - it only registers listeners for these names - but a replay parses
// EVERY frame the server sent, so one newer/unknown event type from a deploy-skewed server
// would reach reduce and wedge the whole fold. Filter to the known set first.
const KNOWN_EVENT_TYPES: ReadonlySet<string> = new Set(ALL_EVENT_TYPES);

export async function replayTaskEvents(taskId: string): Promise<TaskEvent[]> {
  // A fetch can set a header, so the replay authenticates as every other request does; only an
  // EventSource needs a ticket in its URL.
  const token = getToken() ?? "";
  const url = `${API_URL}/v1/tasks/${taskId}/events?since=0&follow=false`;
  const res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
  if (!res.ok) return [];
  const events: TaskEvent[] = [];
  // SSE frames are blank-line separated; the server emits CRLF, so match either ending.
  for (const block of (await res.text()).split(/\r?\n\r?\n/)) {
    const data = block.split(/\r?\n/).find((l) => l.startsWith("data:"));
    if (!data) continue;
    try {
      events.push(JSON.parse(data.slice(5).trim()) as TaskEvent);
    } catch {
      /* skip a malformed frame */
    }
  }
  return events;
}

export function foldEvents(events: TaskEvent[]): TaskView {
  let v = emptyView();
  for (const ev of events) {
    if (!KNOWN_EVENT_TYPES.has(ev.type)) continue; // deploy-skew tolerant; assertNever still pins compile-time exhaustiveness
    v = reduce(v, ev);
  }
  return v;
}
