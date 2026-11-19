/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The right side's publish rule, apart from the hook that holds the surfaces
// (use-right-surface.ts): while the task is live, a new artifact - or a new revision of one -
// opens the reader so the change is SEEN, not discovered later in the rail.
import type { ArtifactView } from "~/lib/task-view";

/** The newest publish the reader may open, keyed by id and revision. Working-role images are
 *  the figure fleet landing mid-run: they stay off the latch, so a concurrent batch of PNGs
 *  never steals the pane from the section being read. */
export function latestPublish(
  artifacts: readonly ArtifactView[],
): { id: string; key: string } | null {
  const latest = artifacts.filter((a) => !(a.role === "working" && a.type === "image")).at(-1);
  if (!latest) return null;
  return { id: latest.id, key: `${latest.id}:${latest.revision ?? 1}` };
}

/** Whether that publish opens the reader: only on a live task, never against a deliberate
 *  close of the same artifact, never for a re-run's superseded output (history, not the
 *  deliverable), and never over an open screen (the person is watching the Machine; the
 *  artifact waits in the rail). */
export function opensOnPublish(
  id: string,
  opts: {
    live: boolean;
    dismissed: string | null;
    superseded: Readonly<Record<string, string>>;
    screenOpen: boolean;
  },
): boolean {
  return (
    opts.live && opts.dismissed !== id && !Object.hasOwn(opts.superseded, id) && !opts.screenOpen
  );
}
