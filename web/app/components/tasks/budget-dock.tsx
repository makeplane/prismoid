/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The budget dock (B3) - the face of a "Budget reached" gate. A TASK ceiling an editor raises
// here: Raise and resume sets the budget, then accepts the gate (an accept re-checks the budget at
// the next boundary, so a raise that still falls short parks again). The prefill clears what
// resuming needs - never below the ceiling, which the server refuses while the gate is open. A
// PROJECT ceiling is the Project owner's: the dock says so (the budget is spent, or the coming wave
// would overrun it), links to the Project's settings, and offers Resume for after they raise it -
// the server refuses (409, the gate stays open) an accept while the Project cannot hold the wave,
// and the toast carries its message; the dock does not pre-check. A
// Project face shows its own copy ALONE: the server's reason tells a task ceiling's "raise it and
// resume", which would contradict it. A WORKSPACE that reached its spending allowance is nobody's
// to raise here: its face is the server's reason and Cancel task alone, and the allowance coming
// back resumes the task on its own (the server refuses an accept while it is still spent). Cancel
// task closes the gate either way.
// Viewers never see a dock (the route shows docks to editors only); the gate's row in the tree
// says it is waiting.

import { Button } from "@makeplane/propel/components/button";
import { Input, InputGroup } from "@makeplane/propel/components/input";
import { Wallet } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { api, toastError, unwrap } from "~/lib/api/client";
import { formatUsd } from "~/lib/format";
import type { BudgetFigures } from "~/lib/api/events";
import type { GateView } from "~/lib/task-view";
import { cn } from "~/lib/utils";
import { DOCK_SHELL } from "./approval-dock";

/** One dollar above what resuming needs, in cents: the live spend plus the coming wave the gate
 *  projected, or the ceiling it hit when that is higher - a prefill under the ceiling would LOWER
 *  it under a "Raise" label. A gate with no figures (parked before them) has only the spend. */
export function suggestedBudget(spentUsd: number, figures?: BudgetFigures | null): string {
  const usd = (x: number | string | undefined) => {
    const n = Number(x);
    return Number.isFinite(n) && n > 0 ? n : 0;
  };
  const needed = Math.max(usd(figures?.ceiling_usd), usd(spentUsd) + usd(figures?.projected_usd));
  return (Math.ceil(needed * 100) / 100 + 1).toFixed(2);
}

/** Which ceiling the gate names, as the dock's face: the task's (raised here), the Project's (its
 *  owner raises it), or the workspace's spending allowance (nothing here restores it). An unknown
 *  or empty scope is the task's, as it always was. */
export function budgetFace(scope: string | undefined): "task" | "project" | "workspace" {
  return scope === "project" || scope === "workspace" ? scope : "task";
}

/** A task ceiling's sentence in the header's money (`formatUsd`) and the live spend the header
 *  shows, not the frozen figure the gate's reason was written with. Null without figures (a gate
 *  parked before them): the server's reason stands. */
export function budgetLine(
  figures: BudgetFigures | null | undefined,
  spentUsd: number,
): string | null {
  const ceiling = Number(figures?.ceiling_usd);
  if (!figures || !Number.isFinite(ceiling) || ceiling <= 0) return null;
  const spent = formatUsd(Math.max(spentUsd, Number(figures.spent_usd) || 0));
  const wave = Number(figures.projected_usd);
  return wave > 0
    ? `${spent} spent; the next wave needs about ${formatUsd(wave)} against the task budget of ${formatUsd(ceiling)}. Raise it and resume, or cancel.`
    : `The task budget of ${formatUsd(ceiling)} is spent (${spent} so far). Raise it and resume, or cancel.`;
}

/** The typed amount as the API's dollars-and-cents string, or null when it is not one above zero. */
export function parseBudget(raw: string): string | null {
  const t = raw.trim().replace(/^\$/, "");
  if (!/^\d+(\.\d{1,2})?$/.test(t)) return null;
  const n = Number(t);
  return n > 0 ? n.toFixed(2) : null;
}

export function BudgetDock({
  gate,
  taskId,
  projectId,
  spentUsd,
  onResolved,
  className,
}: {
  gate: GateView;
  taskId: string;
  projectId: string | null;
  spentUsd: number;
  onResolved?: (resolvedBy?: string, resolvedById?: string | null) => void;
  className?: string;
}) {
  const [amount, setAmount] = useState(() => suggestedBudget(spentUsd, gate.budget_figures));
  const [busy, setBusy] = useState(false);
  const usd = parseBudget(amount);
  const face = budgetFace(gate.budget_scope);
  // A gate the wave projection opened: the spend is under the ceiling, the coming wave is not.
  const wave = Number(gate.budget_figures?.projected_usd) > 0;
  // A task ceiling's words, in the header's money; a workspace face is the server's reason, and a
  // Project face shows its own copy alone.
  const taskLine =
    face === "task"
      ? (budgetLine(gate.budget_figures, spentUsd) ?? gate.reason)
      : face === "workspace"
        ? gate.reason
        : null;

  async function resolve(action: "accept" | "cancel") {
    const res = await unwrap(
      api.POST("/v1/gates/{gate_id}/resolve", {
        params: { path: { gate_id: gate.gate_id } },
        body: { action },
      }),
    );
    onResolved?.(res.resolved_by, res.resolved_by_id);
  }

  async function run(what: "raise" | "resume" | "cancel") {
    if (busy || (what === "raise" && !usd)) return;
    setBusy(true);
    let raised = false;
    try {
      if (what === "raise") {
        await unwrap(
          api.PUT("/v1/tasks/{task_id}/budget", {
            params: { path: { task_id: taskId } },
            body: { usd: usd as string },
          }),
        );
        raised = true;
      }
      await resolve(what === "cancel" ? "cancel" : "accept");
    } catch (e) {
      toastError(
        e,
        what === "raise"
          ? raised
            ? "The budget is raised, but the task couldn't resume."
            : "Couldn't raise the budget."
          : what === "resume"
            ? "Couldn't resume."
            : "Couldn't cancel the task.",
      );
    } finally {
      setBusy(false); // the stacked-gates key-remount law (QuestionDock's)
    }
  }

  return (
    <div className={cn(DOCK_SHELL, "space-y-3 p-3", className)}>
      <div className="flex items-start gap-2">
        <Wallet className="text-muted-foreground mt-0.5 size-4 shrink-0" />
        <div className="space-y-1 text-body-sm-regular">
          <p className="font-medium">{gate.title ?? "Budget reached"}</p>
          {taskLine && <p className="text-muted-foreground">{taskLine}</p>}
          {face === "project" && (
            <p className="text-muted-foreground">
              {wave
                ? "The next wave would take the Project past its monthly budget, and only its owner can raise it"
                : "The Project's monthly budget is spent, and only its owner can raise it"}
              {projectId ? (
                <>
                  {" "}
                  in{" "}
                  <Link to={`/projects/${projectId}?tab=settings`} className="underline">
                    Project settings
                  </Link>
                </>
              ) : null}
              ; Resume works once the owner has raised it.
            </p>
          )}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {face === "project" && (
          <Button
            variant="primary"
            size="md"
            stretch="auto"
            label="Resume"
            onClick={() => void run("resume")}
            disabled={busy}
          />
        )}
        {face === "task" && (
          <>
            <div className="w-28">
              <InputGroup size="md">
                <Input
                  size="md"
                  aria-label="New budget in dollars"
                  inputMode="decimal"
                  value={amount}
                  onChange={(e) => setAmount(e.target.value)}
                />
              </InputGroup>
            </div>
            <Button
              variant="primary"
              size="md"
              stretch="auto"
              label="Raise and resume"
              onClick={() => void run("raise")}
              disabled={busy || !usd}
            />
          </>
        )}
        <span className="ml-auto">
          <Button
            variant="ghost"
            size="md"
            stretch="auto"
            label="Cancel task"
            onClick={() => void run("cancel")}
            disabled={busy}
          />
        </span>
      </div>
    </div>
  );
}
