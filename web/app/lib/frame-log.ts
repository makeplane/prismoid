/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The durable frames a task page has already folded, kept for the life of the tab so that coming
// back to a task folds them locally and asks the server only for what came after (`?since=` the
// newest kept seq), instead of replaying the whole record on every mount.
//
// Why this is safe: task events are append-only, so a seq the page has seen never changes, and
// folding the kept frames from the task's detail gives exactly the view a replay from 0 gives.
// Only durable frames (those with an SSE id) are kept; deltas and presence never are. A log is
// complete from the task's first frame or it does not exist: it is opened only by a mount that
// starts from it, frames are appended only in seq order, and a log that is evicted or grows past
// its cap is dropped whole, so the next mount replays from 0 again. A log belongs to the
// credential that read it: another sign-in in the same tab never sees it (the server redacts
// events per viewer).

export type KeptFrame = { type: string; data: string; seq: number };

type Log = { owner: string; frames: KeptFrame[] };

export type FrameLog = ReturnType<typeof createFrameLog>;

export function createFrameLog({ maxTasks = 4, maxFrames = 5000 } = {}) {
  // Map order is recency: a read moves the task to the end, eviction takes the front.
  const logs = new Map<string, Log>();

  return {
    /** Start (or resume) a task's log for `owner`: the frames already kept, oldest first. */
    open(taskId: string, owner: string): readonly KeptFrame[] {
      const found = logs.get(taskId);
      logs.delete(taskId);
      const log = found && found.owner === owner ? found : { owner, frames: [] };
      logs.set(taskId, log);
      while (logs.size > maxTasks) {
        const oldest = logs.keys().next().value as string;
        logs.delete(oldest);
      }
      return log.frames.slice();
    },

    /** Keep one durable frame. A frame for a log that is gone, of another owner, or not newer
     *  than the newest kept seq is ignored. */
    keep(taskId: string, owner: string, frame: KeptFrame): void {
      const log = logs.get(taskId);
      if (!log || log.owner !== owner) return;
      const newest = log.frames.at(-1)?.seq ?? 0;
      if (frame.seq <= newest) return;
      if (log.frames.length >= maxFrames) {
        logs.delete(taskId);
        return;
      }
      log.frames.push(frame);
    },

    clear(): void {
      logs.clear();
    },
  };
}

/** The `since` a mount asks the server for: the newest kept seq, 0 when nothing is kept. */
export function resumeSeq(frames: readonly KeptFrame[]): number {
  return frames.at(-1)?.seq ?? 0;
}

/** The tab's one log, shared by every mount of the task stream. */
export const taskFrameLog = createFrameLog();
