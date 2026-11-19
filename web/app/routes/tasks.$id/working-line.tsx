/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** Bottom-of-transcript status for the PRE-TREE stretch (created/clarifying, legacy
 * replays): the shared TailLine, anchored on the task's birth - so the transcript ends
 * in ONE breathing shape from first frame to settle. */

import type { StepView } from "~/lib/task-view";
import { workingPhrase } from "./status-copy";
import { TailLine, useLiveElapsed } from "./tail-line";

export function WorkingLine({
  status,
  steps,
  createdAt,
  note,
  className,
}: {
  status: string;
  steps: StepView[];
  /** The task's birth - the pre-tree clock anchors here (no step has started yet). */
  createdAt?: string;
  /** Connection truth ("waiting for signal…") - the tail row carries it now that the
   *  title band is gone. */
  note?: string | null;
  className?: string;
}) {
  // Always active: this surface only renders while the pre-tree stretch is live.
  const time = useLiveElapsed(createdAt, true);
  return (
    <TailLine
      className={className}
      stats={time}
      phrase={workingPhrase(status, steps)}
      note={note}
    />
  );
}
