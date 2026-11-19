/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The context ring - the steering bar's quiet gauge of what this task was told beyond the
// ask. One glyph at rest (a ring, filled to the pack's share of its budget); the popover is
// the receipt: total, then each lane against its own ceiling, "trimmed" where the budget bit.
// Same record the rail renders (`context.assembled`) - this is the at-hand view, not a new one.

import { Popover, PopoverContent, PopoverTrigger } from "@makeplane/propel/components/popover";
import { LinearProgress } from "@makeplane/propel/components/linear-progress";
import { cn } from "~/lib/utils";
import type { TaskView } from "~/lib/task-view";

/** A lane's ceiling as the server stated it on `context.assembled` - never a client copy, so a
 *  budget change on the server needs no web change. Unbudgeted lanes and older events have none. */
export const laneBudget = (lane: { budget?: number | null }) => lane.budget ?? null;

/** The pack's ceiling: the sum of the ceilings its lanes were fitted to; 0 when none was stated. */
export const contextCeiling = (context: TaskView["context"]) =>
  (context?.lanes ?? []).reduce((sum, lane) => sum + (laneBudget(lane) ?? 0), 0);

/** The lanes, as every surface says them (the rail imports this - one wording). */
export const LANE_LABEL: Record<string, string> = {
  instructions: "Project instructions",
  user: "Your memory",
  project: "Project memory",
  bot: "What the bot remembers",
  thread: "This thread",
  skills: "Skill notes",
};

const pct = (part: number, whole: number) => Math.min(100, Math.round((part / whole) * 100));

export function ContextMeter({ context }: { context: TaskView["context"] }) {
  const total = context?.totalTokens ?? 0;
  const ceiling = contextCeiling(context);
  const share = ceiling > 0 ? pct(total, ceiling) : 0;

  return (
    <Popover>
      <PopoverTrigger
        aria-label="What Prismoid remembered for this task"
        // A quiet inline gauge, not a control-height button: the plain element keeps its look.
        render={
          <button
            type="button"
            className="text-muted-foreground/70 hover:text-foreground flex items-center gap-1.5 rounded-md px-1 py-0.5 text-caption-md-regular tabular-nums transition-colors"
          />
        }
      >
        <Ring pct={share} />
        Context
        {total > 0 && <span>· {total} tok</span>}
      </PopoverTrigger>
      <PopoverContent side="top" align="end">
        {/* The width box (Propel's rich panel is w-max) and the kit panel's xs type. */}
        <div className="flex w-72 flex-col gap-3 text-caption-md-regular">
          <div>
            <div className="text-muted-foreground flex items-baseline justify-between gap-2">
              <span>Context injected</span>
              <span className="tabular-nums">
                {total.toLocaleString()}
                {ceiling > 0 && <> / {ceiling.toLocaleString()}</>} tok
              </span>
            </div>
            <div className="mt-1.5">
              <LinearProgress
                value={share}
                size="sm"
                variant="brand"
                showValue={false}
                aria-label="Context injected, of its total budget"
              />
            </div>
          </div>

          {/* Gauges only - the verbatim lines live in the rail's Context section and the
              ledger, one hop away. Hover "trimmed" for what it means. */}
          {context && context.lanes.length > 0 ? (
            <div className="space-y-2.5">
              {context.lanes.map((lane) => {
                const budget = laneBudget(lane);
                return (
                  <div key={lane.lane}>
                    <div className="flex items-baseline justify-between gap-2">
                      <span>{LANE_LABEL[lane.lane] ?? lane.lane}</span>
                      <span className="text-muted-foreground tabular-nums">
                        {lane.truncated && (
                          <span
                            className="text-muted-foreground/70"
                            title="This lane hit its token budget - what didn't fit stayed in the ledger, not the prompt."
                          >
                            trimmed ·{" "}
                          </span>
                        )}
                        {lane.tokens}
                        {budget !== null && <> / {budget.toLocaleString()}</>} tok
                      </span>
                    </div>
                    <div className="mt-1">
                      {budget !== null && (
                        <LinearProgress
                          value={pct(lane.tokens, budget)}
                          size="sm"
                          variant="brand"
                          showValue={false}
                          aria-label={`${LANE_LABEL[lane.lane] ?? lane.lane}, of its ${budget}-token budget`}
                        />
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="text-muted-foreground">
              Nothing beyond your ask reached this task - no project instructions, memories, or
              thread context. Memories build up as tasks deliver; see Settings → Memory.
            </p>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}

/** A 14px donut, filled clockwise from 12 o'clock. The track alone IS the zero state. */
function Ring({ pct: filled, className }: { pct: number; className?: string }) {
  const r = 5;
  const c = 2 * Math.PI * r;
  return (
    <svg viewBox="0 0 14 14" className={cn("size-3.5 -rotate-90", className)} aria-hidden>
      <circle
        cx="7"
        cy="7"
        r={r}
        fill="none"
        strokeWidth="2.5"
        className="stroke-muted-foreground/25"
      />
      {filled > 0 && (
        <circle
          cx="7"
          cy="7"
          r={r}
          fill="none"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeDasharray={`${(filled / 100) * c} ${c}`}
          className="stroke-primary"
        />
      )}
    </svg>
  );
}
