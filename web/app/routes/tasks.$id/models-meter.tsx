/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The models receipt - the steering bar's other quiet gauge: WHICH models this task ran
// and WHERE the spend went. Same grammar as ContextMeter (one glyph at rest, the popover
// is the receipt), and the same figures as the topbar's spend and calls: both read the one
// ledger fold of the task stream (`view.usageLedger`), so the popover can never disagree
// with the header. Rows are named for what a person knows - the model, the step's title.

import { Boxes } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@makeplane/propel/components/popover";
import { Money } from "~/components/kit";
import type { TaskView } from "~/lib/task-view";
import { callsLabel, usageBreakdown } from "~/lib/usage-ledger";
import { spendOf } from "./task-page";

export function ModelsMeter({
  view,
  verifyUsd = 0,
}: {
  /** What checking the answer cost (the receipts' own spend, landed after Delivered): it is
   *  already inside every total below, and named here so the late rise has a label. */
  verifyUsd?: number;
  view: Pick<
    TaskView,
    "usageLedger" | "usageUsd" | "totalUsd" | "usageCalls" | "usageSearches" | "feed"
  >;
}) {
  const { models, steps, units } = usageBreakdown(view);
  const empty = view.usageCalls === 0 && view.usageSearches === 0;

  return (
    <Popover>
      <PopoverTrigger
        aria-label="Models this task ran, and where the spend went"
        // A quiet gauge, not a button (ContextMeter's grammar): the look rides a plain <button>.
        render={
          <button
            type="button"
            className="text-muted-foreground/70 hover:text-foreground flex items-center gap-1.5 rounded-md px-1 py-0.5 text-caption-md-regular tabular-nums transition-colors"
          />
        }
      >
        <Boxes className="size-3.5" />
        Models
      </PopoverTrigger>
      <PopoverContent side="top" align="end">
        {/* Propel's rich panel pads 16px and sizes to content: the column holds the old 320px. */}
        <div className="flex w-72 flex-col gap-3">
          {empty ? (
            <p className="text-muted-foreground text-caption-md-regular">
              No model calls yet - the ledger fills as the task works.
            </p>
          ) : (
            <>
              <div className="text-muted-foreground flex items-baseline justify-between gap-2 text-caption-md-regular">
                <span>{callsLabel(view.usageCalls, view.usageSearches)}</span>
                <Money usd={spendOf(view)} className="tabular-nums" />
              </div>

              {verifyUsd > 0 && (
                <div className="text-muted-foreground flex items-baseline justify-between gap-3 text-caption-md-regular">
                  <span>Verification (included above)</span>
                  <Money usd={verifyUsd} muted className="shrink-0 tabular-nums" />
                </div>
              )}

              <div className="space-y-1">
                <p className="text-muted-foreground text-caption-md-regular font-medium tracking-wide uppercase">
                  By model
                </p>
                {models.map((m) => (
                  <div
                    key={m.model}
                    className="flex items-baseline justify-between gap-3 text-caption-md-regular"
                  >
                    <span className="min-w-0 truncate" title={m.model}>
                      {m.seconds > 0 && m.calls === 0
                        ? `Machine time (${m.model.replace(/^machine\./, "")})`
                        : m.model}
                      {m.calls === 0 && m.searches > 0 ? " (search)" : ""}
                    </span>
                    <Money usd={m.usd} muted className="shrink-0 tabular-nums" />
                  </div>
                ))}
              </div>

              {steps.length > 0 && (
                <div className="space-y-1">
                  <p className="text-muted-foreground text-caption-md-regular font-medium tracking-wide uppercase">
                    By step
                  </p>
                  {steps.map((step) => (
                    <div
                      key={step.id || "none"}
                      className="flex items-baseline justify-between gap-3 text-caption-md-regular"
                    >
                      <span className="min-w-0 truncate" title={step.title}>
                        {step.title}
                      </span>
                      <Money usd={step.usd} muted className="shrink-0 tabular-nums" />
                    </div>
                  ))}
                </div>
              )}

              {units.length > 0 && (
                <div className="space-y-1">
                  <p className="text-muted-foreground text-caption-md-regular font-medium tracking-wide uppercase">
                    Native units
                  </p>
                  {units.map(([unit, n]) => (
                    <div
                      key={unit}
                      className="text-muted-foreground flex items-baseline justify-between gap-3 text-caption-md-regular"
                    >
                      <span className="min-w-0 truncate">{unit.replaceAll("_", " ")}</span>
                      <span className="shrink-0 tabular-nums">{n.toLocaleString()}</span>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
