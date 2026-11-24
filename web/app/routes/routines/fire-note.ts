/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// What Run now says once the server has decided the fire. The fire door answers 202 whatever it
// decided (a refused fire is a result wearing its journaled reason), so the words come from the
// decision, never from the status: only a mint or a hold made a task.

import type { components } from "~/lib/api/types.gen";
import { decisionLabel } from "./streaks";

type FireOut = components["schemas"]["FireOut"];

export type FireNote = { tone: "success" | "info" | "error"; text: string };

export function fireNote(out: Pick<FireOut, "decision" | "detail">): FireNote {
  if (out.decision === "minted") return { tone: "success", text: "Fired - the run shows below." };
  if (out.decision === "held")
    return { tone: "success", text: "Fired - the task waits below until you release it." };
  if (out.decision === "error")
    return {
      tone: "error",
      text: out.detail ? `The fire failed: ${out.detail}` : "The fire failed.",
    };
  return {
    tone: "info",
    text: `Not fired (${decisionLabel(out.decision)}) - the run shows below.`,
  };
}
