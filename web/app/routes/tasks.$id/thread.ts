/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Reconstruct a FORKED task's lineage (a follow-up continues its own task now, so the
// only parent chains are forks and legacy follow-up children). We walk up from the tip's parent,
// replaying each ancestor's durable events (since=0, follow=false) through the same reducer and
// prepending its feed. The tip streams live; ancestors are settled history.

import { foldEvents, replayTaskEvents } from "~/lib/replay";
import type { ArtifactView, FeedItem, StepView } from "~/lib/task-view";

export type Ancestor = {
  taskId: string;
  goal?: string;
  steps: StepView[];
  feed: FeedItem[];
  artifacts: ArtifactView[];
};

/** Ancestors oldest-first (root … immediate parent), each with its rendered feed. */
export async function loadAncestors(parentTaskId: string): Promise<Ancestor[]> {
  const chain: Ancestor[] = [];
  const seen = new Set<string>();
  let cur: string | null | undefined = parentTaskId;
  while (cur && !seen.has(cur)) {
    seen.add(cur);
    const v = foldEvents(await replayTaskEvents(cur));
    chain.unshift({
      taskId: cur,
      goal: v.goal,
      steps: v.steps,
      feed: v.feed,
      artifacts: v.artifacts,
    });
    cur = v.parentTaskId;
  }
  return chain;
}
