/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The task page's small decisions, apart from the component that renders them: what the tail's
// liveness note says, which feed item is the live turn, what Copy copies, which earlier plans the
// rail lists, what the spend gauge reads, which notice stands under the transcript, and what the
// box invites. Pure, so they are tested without a DOM (task-page.test.ts).
import { TERMINAL, type TaskStatus } from "~/lib/api/events";
import type { components } from "~/lib/api/types.gen";
import { systemWaitLimitMs } from "~/lib/stream-stall";
import {
  dockFor,
  steerWhileWaiting,
  type FeedItem,
  type GateView,
  type ScreenLease,
  type StepView,
  type TaskView,
} from "~/lib/task-view";
import type { EarlierPlan } from "./task-rail";

type MachineOut = components["schemas"]["MachineOut"];
type ExecutionItem = Extract<FeedItem, { kind: "execution" }>;

/** The connection truth-string on the tail's one liveness row. Disconnect OUTRANKS stall - a
 *  dropped socket also goes silent, and "reconnecting…" is the truer word for it. The note
 *  reaches the tail in EVERY live status: a task parked at a gate can lose its socket too, and
 *  `waiting` is the one state where no other surface would say so. Silence the page can explain
 *  is not "waiting for signal…" at first: a wait on a PERSON never is (nothing is expected to
 *  arrive), and a wait on the SYSTEM shows its own phrase until it outlasts the longer bound
 *  (`stalledLong`), when the note returns - a worker that died mid-boot stays visible. */
export function liveNoteOf(
  connected: boolean,
  stalled: boolean,
  stalledLong: boolean,
  wait: WaitOn,
): string | null {
  if (!connected) return "reconnecting…";
  if (wait === "person") return null;
  return (wait === "system" ? stalledLong : stalled) ? "waiting for signal…" : null;
}

/** Who the quiet is waiting on, when the page knows: a PERSON (a task parked on a gate, or paused
 *  - the tail says what it waits for), or the SYSTEM (a step holding one in-flight `machine` row
 *  that states the bound on its wait: the Machine start, wake or queue). Anything else silent -
 *  including a Machine row from an older event, with no stated bound - is unexplained: null, and
 *  the plain stall window applies. */
export type WaitOn = "person" | "system" | null;

export function waitOf(view: Pick<TaskView, "status" | "feed">): WaitOn {
  if (view.status === "waiting" || view.status === "paused") return "person";
  return systemWaitLimitMs(view.feed) != null ? "system" : null;
}

/** The live turn's execution item - the tail's data source, found from the tail of the feed
 *  (never by index: inserts would stale it). Null once that turn has settled. */
export function liveExecOf(feed: readonly FeedItem[]): ExecutionItem | null {
  for (let i = feed.length - 1; i >= 0; i--) {
    const f = feed[i];
    if (f.kind === "execution") return f.settled ? null : f;
  }
  return null;
}

/** What Copy puts on the clipboard: the delivered summary, else the newest answer segment. */
export function answerTextOf(
  deliveredSummary: string | null | undefined,
  feed: readonly FeedItem[],
): string {
  if (deliveredSummary) return deliveredSummary;
  for (let i = feed.length - 1; i >= 0; i--) {
    const item = feed[i];
    if (item.kind === "segment" && item.markdown) return item.markdown;
  }
  return "";
}

/** Earlier plans for the rail, nearest first: this task's superseded revisions, then the
 *  thread's ancestor plans (labelled by their ask). */
export function earlierPlansOf(
  taskId: string,
  planHistory: readonly StepView[][],
  ancestors: readonly { taskId: string; goal?: string | null; steps: StepView[] }[],
): EarlierPlan[] {
  const revisions = planHistory
    .map((steps, i) => ({
      id: `${taskId}-rev${i}`,
      label: `Revision ${i + 1} · replaced`,
      steps,
    }))
    .reverse();
  const parents = ancestors
    .filter((a) => a.steps.length > 0)
    .map((a) => ({
      id: a.taskId,
      label: a.goal ?? "Earlier task",
      steps: a.steps,
    }))
    .reverse();
  return [...revisions, ...parents];
}

/** The rail's steps for a task in this status. A settled task runs nothing: a step still wearing
 *  a live state (running, retrying, waiting to start) when the task ended did not finish, and
 *  the rail says so ("skipped") rather than "retrying" beside a stopped task. */
export function railStepsOf(steps: StepView[], status: TaskStatus): StepView[] {
  if (!TERMINAL.has(status)) return steps;
  const unfinished = (s: StepView) => !["done", "escalated", "skipped"].includes(s.state);
  return steps.some(unfinished)
    ? steps.map((s) => (unfinished(s) ? { ...s, state: "skipped" } : s))
    : steps;
}

/** The spend figure: the live ledger once any usage frame lands (the stream replays from 0, so
 *  the fold IS the full sum); the loader snapshot only covers the first paint. */
export function spendOf(view: Pick<TaskView, "usageUsd" | "totalUsd">): string {
  return view.usageUsd != null ? String(view.usageUsd) : view.totalUsd;
}

/** The rail's Usage section, or null while nothing has been spent. */
export function usageOf(
  spendUsd: string,
  calls: number,
  searches: number,
): { usd: string; calls: number; searches: number } | null {
  return calls > 0 || searches > 0 || Number(spendUsd) > 0
    ? { usd: spendUsd, calls, searches }
    : null;
}

/** The Machine as the rail's chip draws it: the lease the page reads, never the loader's row -
 *  the one truth for thumb, pane and chip. */
export function railMachineOf(
  machine: MachineOut | null | undefined,
  lease: ScreenLease | null,
): MachineOut | null {
  if (!machine) return null;
  return {
    ...machine,
    control: lease
      ? {
          holder: lease.holder,
          private: lease.private,
          since: machine.control?.since ?? "",
          until: lease.until ?? "",
        }
      : null,
  };
}

/** What stands under the transcript while the task is live and no gate is open: a held fork
 *  says it is parked on purpose, a created task says "queued" (never impersonating work), a
 *  paused one says so, and the working line covers the stretch before the execution tree
 *  carries its own liveness. Null while answer text or a draft is pouring, or the tree is up. */
export type LiveNotice = "held" | "queued" | "paused" | "working" | null;

export function liveNoticeOf(
  view: Pick<TaskView, "status" | "feed" | "draft" | "execId">,
  held: boolean,
): LiveNotice {
  if (view.status === "created") return held ? "held" : "queued";
  if (view.status === "paused") return "paused";
  // An EMPTY live segment (message.start, no delta yet) must not hide the working line.
  const pouring = view.feed.some(
    (i) => i.kind === "segment" && i.live && (i.markdown?.trim().length ?? 0) > 0,
  );
  if (pouring || (view.draft?.text.trim().length ?? 0) > 0) return null;
  return view.execId != null ? null : "working";
}

/** The box's invitation: an open question takes a direct reply, a held fork waits for its
 *  direction, a live task takes a steer, a settled one a follow-up. */
export function composerPlaceholder(opts: {
  questionOpen: boolean;
  held: boolean;
  live: boolean;
}): string {
  if (opts.questionOpen) return "Or reply directly…";
  if (opts.held) return "Where should this branch go?";
  return opts.live ? "Steer the task…" : "Ask a follow-up…";
}

const PROPOSAL_KINDS = ["document", "collection", "spreadsheet", "deck", "code"] as const;

/** A proposal card's deliverable as the follow-up's dial: only the kinds the wire takes. */
export function proposalKind(
  deliverable: string | null,
): (typeof PROPOSAL_KINDS)[number] | undefined {
  return PROPOSAL_KINDS.find((k) => k === deliverable);
}

/** Whether the header's dial stamps (size, path, document format) may still be missing from
 *  the loader snapshot after planning: a follow-up past the snapshot's turn may have
 *  re-resolved them, an auto size resolves at planning, and a non-fleet thread's path (or a
 *  dialed document's format, from the goal's own words) can land then too. */
export function stampsPending(
  view: Pick<TaskView, "status" | "turn">,
  detail: {
    turn: number;
    scale?: string | null;
    deliverable?: string | null;
    deliverable_format?: string | null;
  },
  fleetThread: boolean,
): boolean {
  const preStamp =
    view.status === "created" || view.status === "clarifying" || view.status === "planning";
  if (preStamp) return false;
  if (view.turn > detail.turn || detail.scale === "auto") return true;
  if (fleetThread) return false;
  return (
    detail.deliverable == null ||
    (detail.deliverable === "document" && detail.deliverable_format == null)
  );
}

/** The open gate as the page stages it. LIVE-gated on purpose: a settled task's content is the
 *  point, and the server closes every open gate at delivery - a gate row that somehow survives
 *  in the view (a historical record, a missed fold) must never wall off a delivered answer.
 *  An open question holds the steps it parks, so an accepted steer applies after the answer
 *  and the note above the dock says so. The CONTROL gate is the one the screen's banner reads. */
export function openGateOf(
  view: Pick<TaskView, "feed">,
  gates: readonly GateView[],
  live: boolean,
  myId?: string,
): {
  gate: GateView | null;
  questionOpen: boolean;
  steerNote: string | null;
  controlGate: GateView | null;
} {
  const gate = live && gates.length > 0 ? gates[0] : null;
  if (!gate) return { gate: null, questionOpen: false, steerNote: null, controlGate: null };
  const questionOpen = dockFor(gate) === "questions";
  return {
    gate,
    questionOpen,
    steerNote: questionOpen ? steerWhileWaiting(view, gate, myId) : null,
    controlGate: gates.find((g) => g.kind === "control") ?? null,
  };
}
