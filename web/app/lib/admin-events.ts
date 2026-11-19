/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The admin console's replay seam. GET /v1/admin/tasks/{id}/events returns the task's
// durable record as RAW wire payloads - identical objects to what the owner's stream frames
// carry - so the admin peek folds them through the SAME pure reducer as replay (foldEvents),
// and an admin's read of a thread can never disagree with the owner's. This file is the
// small validating adapter between `object[]` on the wire and the typed fold: unknown event
// types are already deploy-skew-filtered inside foldEvents; here we only drop things that
// aren't event-shaped at all.

import type { TaskEvent } from "./api/events";
import { foldEvents } from "./replay";
import type { TaskView } from "./task-view";

/** Keep only event-shaped payloads (an object with a string `type`). The fold's own known-type
 *  filter handles deploy skew; this guards against junk that would break the cast entirely. */
export function toTaskEvents(raw: unknown[]): TaskEvent[] {
  const out: TaskEvent[] = [];
  for (const e of raw) {
    if (typeof e === "object" && e !== null && typeof (e as { type?: unknown }).type === "string") {
      out.push(e as unknown as TaskEvent);
    }
  }
  return out;
}

/** The highest durable seq in a payload batch - the admin peek's incremental poll cursor
 *  (`?since=` is exclusive). 0 when nothing carries a seq, so a first poll starts from zero. */
export function maxSeq(raw: unknown[]): number {
  let max = 0;
  for (const e of raw) {
    const seq = (e as { seq?: unknown } | null)?.seq;
    if (typeof seq === "number" && seq > max) max = seq;
  }
  return max;
}

/** Raw admin event payloads → one TaskView, via the app's one replay fold. */
export function foldAdminEvents(raw: unknown[]): TaskView {
  return foldEvents(toTaskEvents(raw));
}
