/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Steering's folds: a steer's acknowledgements under its bubble (accepted, applied, rejected,
// retracted), the artifacts a re-run superseded, and a pause taking hold.

import { personName } from "~/lib/format";
import type { Ev, FoldTable, GateView, SteerAck, TaskView } from "./task-view-types";

/** The dock's note when a steer landed while its question waited: the steer is
 *  accepted but cannot apply until the answer moves the step on - say so, by name ("You" for
 *  your own). Null when no accepted steer is newer than the question. */
export function steerWhileWaiting(
  v: Pick<TaskView, "feed">,
  gate: Pick<GateView, "feedAt">,
  myId?: string,
): string | null {
  const from = gate.feedAt ?? v.feed.length;
  for (let i = v.feed.length - 1; i >= from; i--) {
    const f = v.feed[i];
    if (f.kind === "user" && f.steered && f.ack?.state === "accepted") {
      const who = !f.author || f.author.id === myId ? "You" : f.author.name;
      return `${who} steered while this waited; it applies after the answer`;
    }
  }
  return null;
}

export const ACCEPTED_LINE = "Accepted · applies at the next safe point";

/** Where an applied steer took hold, by safe point - the step's title in the user's words. */
const APPLIED: Record<string, (title: string) => string> = {
  before_model: (t) => `Applied to ${t} before its next model call`,
  before_tool: (t) => `Applied to ${t} before its next tool call`,
  final_answer: (t) => `Applied to ${t} before it finished`,
  approval: (t) => `Superseded the approval ${t} was waiting on`,
  replan: () => "Re-planned the remaining steps",
  rerun: (t) => `${t} is going again with it`,
  interrupt: () => "Cut a reply short",
};

/** The server's rejection reasons, shortened for the line under the bubble. */
const REJECTED: Record<string, string> = {
  "the task was cancelled before this applied": "Not applied: the task was cancelled first",
  "the task failed before this applied": "Not applied: the task failed first",
  "the turn ended before this applied": "Not applied: the turn ended first",
};

function sentenceCase(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function appliedLine(safePoint: string, stepTitle?: string): string {
  const title = stepTitle?.trim() || "the step";
  const phrase = APPLIED[safePoint];
  return sentenceCase(phrase ? phrase(title) : `Applied to ${title}`);
}

/** A tool's journal name as a person reads it: `notesdemo_write_note` → "notesdemo write note". */
function spokenTool(journal: string): string {
  return journal.replace(/[._]+/g, " ").trim();
}

/** What the steer could not change: the calls that had already acted outside the task. */
function alreadyLine(done: readonly string[] | undefined): string {
  return done?.length
    ? ` · Already done before your change: ${done.map(spokenTool).join(", ")} - ask for a follow-up to correct it`
    : "";
}

function rejectedLine(reason: string): string {
  return REJECTED[reason] ?? `Not applied: ${reason}`;
}

/** Set the acknowledgement on the steer bubble whose command id matches (the wire's canonical
 *  uuid strings, compared as strings). No bubble, no change. */
function withAck(v: TaskView, commandId: string, ack: SteerAck): Pick<TaskView, "feed"> {
  let hit = false;
  const feed = v.feed.map((f) => {
    if (f.kind !== "user" || f.commandId !== commandId) return f;
    hit = true;
    return { ...f, ack };
  });
  return { feed: hit ? feed : v.feed };
}

/** Where the steer took hold. A re-run lists the artifacts it superseded: they
 *  grey wherever they are drawn and never open as the delivered card. */
function steerApplied(v: TaskView, ev: Ev<"steer.applied">): TaskView {
  const fresh = (ev.superseded ?? []).filter((id) => !Object.hasOwn(v.superseded, id));
  const by = ev.author?.trim() ? personName(ev.author) : "a teammate";
  return {
    ...v,
    ...withAck(v, ev.command_id, {
      state: "applied",
      line: appliedLine(ev.safe_point, ev.step_title) + alreadyLine(ev.already_done),
    }),
    superseded:
      fresh.length > 0
        ? {
            ...v.superseded,
            ...Object.fromEntries(fresh.map((id) => [id, by])),
          }
        : v.superseded,
  };
}

function steerRejected(v: TaskView, ev: Ev<"steer.rejected">): TaskView {
  return {
    ...v,
    ...withAck(v, ev.command_id, {
      state: "rejected",
      line: rejectedLine(ev.reason),
    }),
  };
}

function steerRetracted(v: TaskView, ev: Ev<"steer.retracted">): TaskView {
  return {
    ...v,
    ...withAck(v, ev.command_id, {
      state: "retracted",
      line: ev.actor ? `Retracted by ${personName(ev.actor.name)}` : "Retracted",
    }),
  };
}

/** The pause took hold: "Pausing…" becomes "Paused by Ada". */
function pauseApplied(v: TaskView): TaskView {
  return v.pause && !v.pause.applied ? { ...v, pause: { ...v.pause, applied: true } } : v;
}

export const STEER_FOLDS: FoldTable<
  "steer.applied" | "steer.rejected" | "steer.retracted" | "pause.applied"
> = {
  "steer.applied": steerApplied,
  "steer.rejected": steerRejected,
  "steer.retracted": steerRetracted,
  "pause.applied": pauseApplied,
};
