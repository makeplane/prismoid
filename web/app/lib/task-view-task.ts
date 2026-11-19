/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The task's own folds: its opening ask, status moves (with the human's control lines), label,
// budget and project, delivery (the summary, the epilogue and its receipt), the next turn, and
// the answerer's proposal card.

import type { TaskStatus } from "~/lib/api/events";
import { personName } from "~/lib/format";
import { settleExec, turnReceipt } from "./task-view-exec";
import {
  authorOf,
  foldIntoLastSegment,
  insertBeforeStepChrome,
  squash,
  summaryAlreadyShown,
  withControl,
} from "./task-view-feed";
import type { Ev, FeedItem, FoldTable, TaskView } from "./task-view-types";

function taskCreated(v: TaskView, ev: Ev<"task.created">): TaskView {
  const parentTaskId = ev.parent_task_id ?? v.parentTaskId;
  const author = authorOf(ev.actor);
  const creatorId = ev.actor?.id ?? v.creatorId;
  // The opening goal is the user's first turn in the conversation.
  if (!ev.goal) return { ...v, parentTaskId, creatorId, goal: v.goal };
  return {
    ...v,
    parentTaskId,
    creatorId,
    goal: ev.goal,
    feed: [
      ...v.feed,
      // opening: the task's own ask - the transcript hangs the attachment chips here
      {
        kind: "user",
        id: `u${v.nextId}`,
        text: ev.goal,
        opening: true,
        ...(author ? { author } : {}),
      },
    ],
    nextId: v.nextId + 1,
  };
}

/** No marker: the intake round is machinery - the tree's phase row
 *  ("Clarifier · Reviewing your request") narrates it live, the QA card records the
 *  outcome, and the status chip carries the state. The old amber line was a leftover
 *  "warning" from when this demanded attention; it never was one. */
function taskClarifying(v: TaskView): TaskView {
  return { ...v, status: "clarifying" };
}

/** A human's pause, resume or cancel names its actor; a worker's status
 *  change names no one and stays as quiet as it was. `running` is a RESUME only right
 *  after a pause. */
function statusLine(who: string | null, status: TaskStatus, was: TaskStatus): string | null {
  if (!who) return null;
  if (status === "paused") return `${who} paused`;
  if (status === "running" && was === "paused") return `${who} resumed`;
  if (status === "cancelled") return `${who} cancelled the task`;
  return null;
}

/** The pause line's state: "Pausing… (Ada)" from the ask until `pause.applied`
 *  says it took hold. Any other status ends it (the wrapper in `reduce`). */
function pauseOf(v: TaskView, status: TaskStatus, who: string | null): TaskView["pause"] {
  if (status !== "paused" || !who) return v.pause;
  return v.status === "paused" && v.pause ? v.pause : { by: who, applied: false };
}

function taskStatus(v: TaskView, ev: Ev<"task.status">): TaskView {
  const terminal = ev.status === "delivered" || ev.status === "failed" || ev.status === "cancelled";
  const who = ev.actor ? personName(ev.actor.name) : null;
  const next = withControl(
    {
      ...v,
      status: ev.status,
      ending: ev.status === "failed" ? (ev.ending ?? null) : v.ending,
      pause: pauseOf(v, ev.status, who),
      ...(terminal ? { draft: null, ...settleExec(v) } : {}),
    },
    statusLine(who, ev.status, v.status),
  );
  if (ev.status === "failed" && ev.ending === "could_not")
    return { ...next, feed: couldNotFeed(next.feed) };
  return ev.status === "cancelled" && ev.summary ? withStop(next, who, ev) : next;
}

/** The turn's escalations, once the task failed `could_not`: framed as a could-not, not a fault. */
function couldNotFeed(feed: FeedItem[]): FeedItem[] {
  const turnStart = feed.map((f) => f.kind).lastIndexOf("user");
  return feed.map((f, i) =>
    i > turnStart && f.kind === "escalation" ? { ...f, couldNot: true } : f,
  );
}

/** A stopped turn ends on its account: why it stopped and what it finished, never silence. */
function withStop(v: TaskView, who: string | null, ev: Ev<"task.status">): TaskView {
  const id = `ep${v.nextId}`;
  const stopped = { by: who, reason: ev.reason ?? "", account: ev.summary ?? "" };
  const feed: FeedItem[] = [
    ...v.feed,
    {
      kind: "epilogue",
      id,
      delivered: false,
      stopped,
      remembered: [],
      route: v.route ?? undefined,
    },
  ];
  return { ...v, feed, epilogueId: id, nextId: v.nextId + 1 };
}

function taskMoved(v: TaskView, ev: Ev<"task.moved">): TaskView {
  return { ...v, shared: ev.to_project_id != null };
}

/** A label change is list chrome, not transcript content - but the SIDEBAR is list
 *  chrome, and it reads the view through the window patch. */
function taskRenamed(v: TaskView, ev: Ev<"task.renamed">): TaskView {
  return withControl(
    { ...v, title: ev.title ?? v.title },
    ev.actor ? `${personName(ev.actor.name)} renamed the task` : null,
  );
}

function taskBudgetSet(v: TaskView, ev: Ev<"task.budget_set">): TaskView {
  const who = ev.actor ? personName(ev.actor.name) : null;
  const verb =
    ev.previous_usd == null
      ? "set"
      : Number(ev.usd) < Number(ev.previous_usd)
        ? "lowered"
        : "raised";
  return withControl(v, who ? `${who} ${verb} the budget to $${ev.usd}` : null);
}

function taskCancelled(v: TaskView): TaskView {
  return {
    ...v,
    status: "cancelled",
    draft: null,
    gates: [],
    ...settleExec(v),
  };
}

/** Land the delivery summary unless the feed already shows it. ORDER MATTERS:
 *  the multi-step summary is the narrations JOINED, so it always
 *  CONTAINS the last narration - fold-first let longest-wins swell that segment into the
 *  whole join and the receipts told twice. Already-shown goes first: when the feed carries
 *  every block piecewise there is nothing to add; fold stays for the single-bubble upgrade
 *  (express: summary is the narration, possibly longer). */
function withSummary(
  feed: FeedItem[],
  summary: string | null | undefined,
  nextId: number,
): { feed: FeedItem[]; nextId: number } {
  if (!summary) return { feed, nextId };
  const folded = summaryAlreadyShown(feed, summary) ? feed : foldIntoLastSegment(feed, summary);
  if (folded) return { feed: folded, nextId };
  // Same lane-parity rule as step.narration: the summary lands before the chrome.
  return {
    feed: insertBeforeStepChrome(feed, {
      kind: "segment",
      id: `s${nextId}`,
      agent: "assistant",
      markdown: summary,
      live: false,
    }),
    nextId: nextId + 1,
  };
}

/** The summary is normally the narration the feed already carries - dumping it into the
 *  marker painted a wall of raw markdown into a one-line chip. The marker is the quiet
 *  stamp; a summary the feed doesn't already show lands first, as rendered markdown.
 *  Delivery also settles the execution tree - it folds to its one-line summary. */
function taskDelivered(v: TaskView, ev: Ev<"task.delivered">): TaskView {
  const settled = settleExec(v);
  const { feed: summarized, nextId } = withSummary(settled.feed.slice(), ev.summary, v.nextId);
  const feed = summarized.slice();
  const epilogueId = `ep${nextId}`;
  feed.push({
    kind: "epilogue",
    id: epilogueId,
    delivered: true,
    partial: ev.ending === "partial",
    couldNot: ev.ending === "could_not",
    remembered: [],
    receipt: turnReceipt(feed, v.turn),
    route: v.route ?? undefined,
  });
  return {
    ...v,
    status: "delivered",
    ending: ev.ending ?? null,
    delivered: true,
    deliveredSummary: ev.summary,
    // The book is closed: the server resolves every open gate at delivery,
    // and the view agrees even if a resolution frame went missing - a question must
    // never wall off a delivered answer.
    gates: [],
    feed,
    execId: settled.execId,
    epilogueId,
    nextId: nextId + 1,
  };
}

/** The composer's optimistic bubble for this turn: by its send token first, by text as the
 *  fallback. -1 when the frame came from elsewhere (another window, the CLI). */
function optimisticIndex(feed: FeedItem[], ev: Ev<"task.continued">): number {
  const optId = ev.client_token ? `opt-${ev.client_token}` : null;
  const i = optId ? feed.findIndex((f) => f.kind === "user" && f.id === optId) : -1;
  if (i !== -1) return i;
  return feed.findIndex(
    (f) => f.kind === "user" && f.pending === true && squash(f.text) === squash(ev.message),
  );
}

/** The user's next turn on the SAME task: reconcile the optimistic bubble the
 *  composer painted - by its send token first, by text as the fallback - so the row keeps
 *  its identity and never remounts; a genuinely new frame (another window, the CLI)
 *  appends. The frame doubles as the status signal, and the current plan rolls into
 *  history: the new turn's task.planned/step.state build the next one.
 *  The old turn's execution tree settles before the new bubble lands - its groups
 *  are frozen snapshots, so the `steps: []` reset below can't blank it. */
function taskContinued(v: TaskView, ev: Ev<"task.continued">): TaskView {
  const feed = settleExec(v).feed.slice();
  const i = optimisticIndex(feed, ev);
  // The frame's actor is the turn's author: the optimistic row gains it
  // on reconcile, a frame from another window arrives wearing it.
  const author = authorOf(ev.actor);
  const bubble: FeedItem = {
    kind: "user",
    id: i !== -1 ? feed[i].id : `u${v.nextId}`,
    text: ev.message,
    ...(author ? { author } : {}),
  };
  if (i !== -1) feed[i] = bubble;
  else feed.push(bubble);
  return {
    ...v,
    // a proposal card never outlives its turn
    feed: feed.map((f) => (f.kind === "proposal" && f.open ? { ...f, open: false } : f)),
    execId: null, // the next turn's machinery mints a fresh execution item
    epilogueId: null,
    route: null,
    nextId: i === -1 ? v.nextId + 1 : v.nextId,
    status: "planning",
    turn: ev.turn,
    delivered: false,
    ending: null,
    deliveredSummary: undefined,
    draft: null,
    gates: [],
    feedback: null, // a new turn grades fresh
    planHistory: v.steps.length > 0 ? [...v.planHistory, v.steps] : v.planHistory,
    steps: [],
    // the epoch this turn opened at: a steer typed before it is stale
    steerEpoch: Math.max(v.steerEpoch, ev.epoch ?? 0),
  };
}

/** The answerer's soft hatch: a card under the reply, nothing executed. */
function taskProposed(v: TaskView, ev: Ev<"task.proposed">): TaskView {
  return {
    ...v,
    feed: [
      ...v.feed,
      {
        kind: "proposal",
        id: `p${v.nextId}`,
        goal: ev.goal,
        deliverable: ev.deliverable ?? null,
        why: ev.why ?? "",
        turn: ev.turn ?? v.turn,
        open: true,
      },
    ],
    nextId: v.nextId + 1,
  };
}

/** The user's grade on the answer - the newest frame wins; `task.continued` resets it. */
function taskFeedback(v: TaskView, ev: Ev<"task.feedback">): TaskView {
  return { ...v, feedback: ev.rating };
}

/** Pure timing marks for the hop ledger (`just range`); the thread renders
 *  nothing - the record is their whole audience. */
function taskDequeued(v: TaskView): TaskView {
  return v;
}

export const TASK_FOLDS: FoldTable<
  | "task.created"
  | "task.clarifying"
  | "task.status"
  | "task.moved"
  | "task.renamed"
  | "task.budget_set"
  | "task.cancelled"
  | "task.delivered"
  | "task.continued"
  | "task.proposed"
  | "task.feedback"
  | "task.dequeued"
> = {
  "task.created": taskCreated,
  "task.clarifying": taskClarifying,
  "task.status": taskStatus,
  "task.moved": taskMoved,
  "task.renamed": taskRenamed,
  "task.budget_set": taskBudgetSet,
  "task.cancelled": taskCancelled,
  "task.delivered": taskDelivered,
  "task.continued": taskContinued,
  "task.proposed": taskProposed,
  "task.feedback": taskFeedback,
  "task.dequeued": taskDequeued,
};
