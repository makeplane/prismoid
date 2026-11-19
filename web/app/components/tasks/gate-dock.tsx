/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The gate's one face, for every host that answers a gate in place - the task page, the peek,
// a bot chat's work card. `dockFor` decides; this is its only reader that mounts a dock, so a
// new gate kind is one arm here and never a hand-rolled ternary per host (the peek's mounted
// the question stepper for EVERY gate and crashed on approve, connect and budget gates).

import { forwardRef, type ForwardedRef } from "react";
import { ApprovalDock } from "~/components/tasks/approval-dock";
import { BudgetDock } from "~/components/tasks/budget-dock";
import { ConnectDock } from "~/components/tasks/connect-dock";
import { QuestionDock, type QuestionDockHandle } from "~/components/tasks/question-dock";
import { dockFor, type GateView } from "~/lib/task-view";

export const GateDock = forwardRef(function GateDock(
  {
    gate,
    onResolved,
    isCreator,
    creatorName,
    budget,
    className,
  }: {
    gate: GateView;
    /** Fired once the resolve lands, with WHO the server says resolved it (see QuestionDock). */
    onResolved?: (resolvedBy?: string, resolvedById?: string | null, action?: string) => void;
    /** The approve face's access (`approvalAccess`): is the viewer the task's creator? */
    isCreator?: boolean;
    creatorName?: string | null;
    /** What the budget face raises: the task, its Project and its LIVE spend (a stale figure
     *  prefills under the ceiling). A host without them (a bot chat's card - a budget gate never
     *  posts to a conversation) gives a budget park no face. */
    budget?: { taskId: string; projectId: string | null; spentUsd: number };
    className?: string;
  },
  // The question stepper's handle - the composer's "Or reply directly…" lands through it.
  ref: ForwardedRef<QuestionDockHandle>,
) {
  switch (dockFor(gate)) {
    case "connect":
      return <ConnectDock gate={gate} onResolved={onResolved} className={className} />;
    case "approve":
      return (
        <ApprovalDock
          gate={gate}
          onResolved={onResolved}
          isCreator={isCreator}
          creatorName={creatorName}
          className={className}
        />
      );
    case "budget":
      return budget ? (
        <BudgetDock gate={gate} {...budget} onResolved={onResolved} className={className} />
      ) : null;
    case "questions":
      return <QuestionDock ref={ref} gate={gate} onResolved={onResolved} className={className} />;
    case "control":
      // A hand-back answers a CONTROL gate, never this dock: the task page draws `ControlDock`
      // beside its screen pane (a viewer watches too), and a host with no screen draws nothing.
      return null;
    case "none":
    case null: // a pause gate: the task's resume lifts it, no one answers it
      return null;
  }
});
