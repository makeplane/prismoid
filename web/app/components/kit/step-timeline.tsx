/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { cn } from "~/lib/utils";
import { StatusDot, statusLabel } from "./status-badge";

/** The shape a timeline row needs - structurally satisfied by the task view's StepView. */
export type TimelineStep = {
  id: string;
  title: string;
  state: string;
  agent?: string | null;
  attempt: number;
  detail?: string;
  /** One's reason for routing the step to this agent - rendered only when present. */
  why?: string;
};

/** States that mean "in motion right now". In a plan that was replaced they are no longer true. */
const IN_MOTION = new Set(["planning", "running", "verifying", "retrying", "waiting"]);

/** The plan as a connected timeline: a toned dot per step (pulsing while running) on a hairline
 *  thread, the title, and a quiet meta line (agent · state). Built for the rail's narrow measure -
 *  state lives in the dot and the meta line, not a badge per row. The dots share one fixed
 *  column and the thread runs dot to dot, so every title and meta line starts on one edge.
 *  `settled` draws a plan that is history (an earlier revision): nothing ripples, the words go
 *  quiet, and a step that was still in motion when the plan was replaced says so. */
export function StepTimeline({
  steps,
  settled = false,
}: {
  steps: TimelineStep[];
  settled?: boolean;
}) {
  if (steps.length === 0) {
    return <p className="text-muted-foreground text-caption-md-regular">Planning…</p>;
  }
  return (
    <ol>
      {steps.map((s, i) => {
        const last = i === steps.length - 1;
        const state = settled && IN_MOTION.has(s.state) ? "replaced" : statusLabel(s.state);
        return (
          <li key={s.id} className="flex gap-3">
            {/* h-5 = the title's line box: the dot sits on the first line's centre */}
            <div className="flex w-2 shrink-0 flex-col items-center">
              <span className="flex h-5 items-center">
                <StatusDot
                  status={settled && IN_MOTION.has(s.state) ? "cancelled" : s.state}
                  still={settled}
                  decorative
                />
              </span>
              {!last && <div className="bg-border w-px flex-1" />}
            </div>
            <div className={cn("min-w-0 flex-1", !last && "pb-4")}>
              <div className="flex items-start gap-1.5">
                <span
                  title={s.title}
                  className={cn(
                    "line-clamp-2 text-body-sm-regular",
                    s.state === "running" && !settled && "font-medium",
                    settled && "text-muted-foreground",
                    s.state === "skipped" && "text-muted-foreground line-through",
                  )}
                >
                  {s.title}
                </span>
                {s.attempt > 1 && (
                  <span className="bg-warning/10 text-warning mt-0.5 shrink-0 rounded-xs px-1 text-[0.625rem] font-medium tabular-nums">
                    ×{s.attempt}
                  </span>
                )}
              </div>
              <p className="text-muted-foreground mt-0.5 truncate text-caption-md-regular">
                {s.agent ? (
                  <>
                    <span className="capitalize">{s.agent}</span> · {state}
                  </>
                ) : (
                  state
                )}
              </p>
              {s.why && (
                // One's routing reason, in the user's terms - why THIS agent got the step.
                // line-clamp keeps the rail's measure; the title carries the whole sentence.
                <p
                  className="text-muted-foreground/70 mt-0.5 line-clamp-2 text-caption-md-regular italic"
                  title={s.why}
                >
                  {s.why}
                </p>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
