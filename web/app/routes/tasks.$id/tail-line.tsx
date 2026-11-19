/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The ONE tail line - the transcript's last breathing row, in both of its homes (the
 *  tree's live tail and the pre-tree WorkingLine): mark · time [· tokens] · what it's
 *  doing [· connection note], the whole line echoed as a title tooltip for the day the
 *  column truncates it. Extracted because the two homes had drifted (one carried the
 *  tooltip, the other the stats slot; one hand-rolled the tick the other packaged) - the
 *  tail grammar is a law, so it has one shape. */

import { useEffect, useState } from "react";
import type { ExecutionItem, GateView, TaskView } from "~/lib/task-view";
import { Liveness } from "~/components/kit";
import { formatSeconds } from "~/lib/time";
import { doingLine, turnTokens, type PauseView } from "./progress-copy";
import { cn } from "~/lib/utils";

/** The tail's LIVE clock - elapsed since `startedAt`, ticking once a second (the
 *  sanctioned liveness: a number updating). Null when inactive or unanchored - the
 *  frozen durations take over at rest. Moved here from progress-tree with the tail
 *  (working-line kept a hand-rolled twin of this interval); the contract is unchanged. */
export function useLiveElapsed(startedAt: string | undefined, active: boolean): string | null {
  const [, tick] = useState(0);
  useEffect(() => {
    if (!active || !startedAt) return;
    const id = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, [active, startedAt]);
  if (!active || !startedAt) return null;
  const ms = Date.now() - Date.parse(startedAt);
  // Whole seconds while ticking - a live clock jumping tenths is jitter, not signal.
  return Number.isFinite(ms) && ms >= 1000 ? formatSeconds(ms / 1000) : null;
}

export function TailLine({
  stats,
  phrase,
  note,
  className,
}: {
  /** The tabular half - "24s · 6.4k tok" - absent until something has ticked. */
  stats?: string | null;
  /** WHAT is happening - the one aria-live region (only the phrase announces: a ticking
   *  clock in a live region spams screen readers). */
  phrase: string;
  /** Connection truth ("reconnecting…", "waiting for signal…") - the tail is the ONE
   *  liveness home since the title band left. */
  note?: string | null;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "text-muted-foreground flex w-full items-center gap-2 text-body-sm-regular",
        className,
      )}
      title={[stats, phrase].filter(Boolean).join(" · ")}
    >
      <Liveness className="size-3.5" />
      {stats && <span className="shrink-0 tabular-nums">{stats} ·</span>}
      <span className="min-w-0 truncate" aria-live="polite" aria-atomic="true">
        {phrase}
      </span>
      {note && (
        <span className="text-muted-foreground/70 shrink-0 text-caption-md-regular">{note}</span>
      )}
    </div>
  );
}

/** The live turn's tail, computed from its execution item - lifted OUT of the tree
 *  (round five, owner: a streaming artifact card rendered BELOW the tree left the
 *  breathing line stranded above it). The route mounts this as the transcript's LAST
 *  element, after the draft card; while a draft pours, the phrase names the file being
 *  written. Settled items render nothing - the settled header keeps the books. */
export function ExecTail({
  item,
  live,
  status,
  note,
  draftName,
  pause,
  gate,
  className,
}: {
  item: ExecutionItem;
  live: TaskView["live"];
  status?: string;
  note?: string | null;
  /** A person's pause: "Pausing…" until it takes hold, then "Paused" - unnamed; the
   *  transcript's status line carries who. */
  pause?: PauseView | null;
  /** The streaming deliverable's basename - overrides the doing phrase while it pours. */
  draftName?: string | null;
  /** The open gate a waiting task waits on: a "Spending allowance reached" one is named as such. */
  gate?: GateView | null;
  className?: string;
}) {
  const turnElapsed = useLiveElapsed(
    item.groups.find((g) => g.startedAt)?.startedAt,
    !item.settled,
  );
  if (item.settled) return null;
  const heartbeat = live && live.elapsed_s > 0 ? formatSeconds(live.elapsed_s) : null;
  const stats = [turnElapsed ?? heartbeat, turnTokens(item)].filter(Boolean).join(" · ");
  const phrase = draftName ? `Drafting ${draftName}` : doingLine(item, status, pause, gate);
  return <TailLine stats={stats} phrase={phrase} note={note} className={className} />;
}
