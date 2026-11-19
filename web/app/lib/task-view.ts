/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The pure fold: TaskEvent[] → one TaskView. Kept separate from the hook so it's trivially testable
// and has no timing/DOM concerns. Durable (Lane-1) frames are deduped by `seq`, so replaying from
// `since=0` - or resuming after a reconnect - never double-applies (critically, usage counts). Lane-2
// deltas carry no seq and are always applied live.
//
// This file is the facade: the view's shapes live in task-view-types.ts, the shared fold helpers in
// task-view-feed.ts and task-view-exec.ts, and each event family's reducers in their own
// task-view-<family>.ts. Everything a surface imports comes from here.

import { EPHEMERAL, type TaskEvent, type TaskStatus } from "~/lib/api/events";
import type { components } from "~/lib/api/types.gen";
import { ARTIFACT_FOLDS } from "./task-view-artifacts";
import { GATE_FOLDS } from "./task-view-gates";
import { MACHINE_FOLDS } from "./task-view-machine";
import { MESSAGE_FOLDS } from "./task-view-messages";
import { PLAN_FOLDS } from "./task-view-plan";
import { STEER_FOLDS } from "./task-view-steer";
import { TASK_FOLDS } from "./task-view-task";
import { USAGE_FOLDS } from "./task-view-usage";
import { EMPTY_LEDGER } from "./usage-ledger";
import type {
  Ev,
  FeedItem,
  Fold,
  FoldTable,
  ScreenLease,
  TaskView,
  TurnRoute,
} from "./task-view-types";

export type {
  ArtifactView,
  ExecGroup,
  ExecRow,
  ExecutionItem,
  FeedItem,
  FeedTone,
  GateView,
  MarkerGlyph,
  ScreenLease,
  SteerAck,
  StepView,
  TaskView,
  TurnRoute,
  Verification,
} from "./task-view-types";
export { dockFor, gatesFor } from "./task-view-gates";
export { steerWhileWaiting } from "./task-view-steer";

type TaskDetailOut = components["schemas"]["TaskDetailOut"];
type ScreenControlOut = components["schemas"]["ScreenControlOut"];

const EMPTY: Omit<TaskView, "status" | "totalUsd"> = {
  goal: undefined,
  steps: [],
  planHistory: [],
  feed: [],
  execId: null,
  epilogueId: null,
  artifacts: [],
  verification: null,
  gates: [],
  usageCalls: 0,
  usageSearches: 0,
  usageLedger: EMPTY_LEDGER,
  usageUsd: null,
  lateUsd: {},
  verifyUsd: 0,
  live: null,
  draft: null,
  context: null,
  appliedSeq: 0,
  nextId: 1,
  machineVersion: 0,
  machineProfile: null,
  machineTurn: null,
  screenAction: null,
  screen: null,
  screenVersion: 0,
  controlEnded: null,
  delivered: false,
  ending: null,
  parentTaskId: undefined,
  shared: false,
  turn: 1,
  feedback: null,
  route: null,
  steerEpoch: 0,
  pause: null,
  superseded: {},
  pauseGates: {},
  deviceAway: null,
};

function leaseOf(control: ScreenControlOut | null | undefined): ScreenLease | null {
  return control
    ? {
        holder: { id: control.holder.id, name: control.holder.name },
        private: control.private,
        until: control.until,
      }
    : null;
}

/** `screen` as the task read says it is: the repair for a `screen.state` that arrived out of order. */
export function reseedScreen(v: TaskView, detail: TaskDetailOut): TaskView {
  return { ...v, screen: leaseOf(detail.machine?.control) };
}

export function initialView(detail: TaskDetailOut): TaskView {
  return {
    ...EMPTY,
    status: detail.status as TaskStatus,
    totalUsd: detail.cost.total_usd,
    goal: detail.goal,
    title: detail.title ?? undefined,
    // Seed the plan for an instant paint; step.state replays upsert these by id (no duplication).
    steps: (detail.plan.steps ?? []).map((s) => ({
      id: s.id,
      title: s.title,
      state: s.state,
      agent: s.agent,
      attempt: s.attempt,
      why: s.why || undefined,
    })),
    delivered: detail.status === "delivered",
    shared: detail.project_id != null,
    // The record replays from its first frame and each `task.continued` carries its own turn, so a
    // view starts at turn 1 whatever turn the task is on now: seeding the latest turn here stamped
    // every earlier turn's trees with it, and the newest receipt then totalled the whole thread.
    turn: 1,
    steerEpoch: detail.plan_epoch ?? 0,
    screen: leaseOf(detail.machine?.control),
    // A page loaded while a step waits for its device says so at once:
    // the task read names the wait; the replayed `gate.opened` then pins its gate.
    deviceAway: detail.waiting_for ? { gate_id: "", title: detail.waiting_for } : null,
  };
}

/** What the task made, as cards to open - every artifact card in the feed but the ones a
 *  steer's re-run superseded: those are history the transcript keeps, never a
 *  delivered card elsewhere (the inbox's "What it made"). */
export function deliveredArtifacts(
  v: Pick<TaskView, "feed" | "superseded">,
): Extract<FeedItem, { kind: "artifact" }>[] {
  return v.feed.filter(
    (f): f is Extract<FeedItem, { kind: "artifact" }> =>
      f.kind === "artifact" && !Object.hasOwn(v.superseded, f.artifactId),
  );
}

/** The composer's optimistic turn: painted before the wire answers, reconciled in
 *  place by the durable `task.continued` frame - same id, so the bubble never remounts. */
export function addOptimisticUser(v: TaskView, text: string, token: string): TaskView {
  return {
    ...v,
    feed: [...v.feed, { kind: "user", id: `opt-${token}`, text, pending: true }],
  };
}

/** The failure path: the send never landed, so the bubble comes back off. */
export function removeOptimistic(v: TaskView, token: string): TaskView {
  return {
    ...v,
    feed: v.feed.filter((f) => !(f.kind === "user" && f.id === `opt-${token}`)),
  };
}

/** A blank view for replaying an ancestor task's events (no detail available). */
export function emptyView(): TaskView {
  return { ...EMPTY, status: "created", totalUsd: "0" };
}

export { answerLabel, failedCaveat, verdictLine, verdictSentence } from "./verdict-copy";

/** Events that move a step's hold on the Machine with no machine.* frame to announce it - a step
 *  taking a warm Machine (its acquire's `end`, tool `machine`: idle → running), a release to idle,
 *  a park - so they bump `machineVersion` and the chip is re-read. */
function movesMachine(ev: TaskEvent): boolean {
  switch (ev.type) {
    case "activity":
      return ev.tool === "machine" && ev.phase === "end";
    case "task.delivered":
    case "gate.opened":
      return true;
    case "task.status":
      return ev.status === "failed" || ev.status === "cancelled";
    case "step.state":
      return (
        ev.state === "done" ||
        ev.state === "retrying" ||
        ev.state === "escalated" ||
        ev.state === "skipped"
      );
    default:
      return false;
  }
}

export function reduce(v: TaskView, ev: TaskEvent): TaskView {
  let out = reduceEvent(v, ev);
  // A pause lives only while the task says paused: a resume, a new turn, a cancel or a
  // delivery each end it, whichever event carried the status.
  if (out.pause && out.status !== "paused") out = { ...out, pause: null };
  // The server opens the device-away gate BEFORE it moves the task to paused, so the wait is
  // lifted only by a task that was paused leaving it, never by the status it has not reached yet.
  if (out.deviceAway && v.status === "paused" && out.status !== "paused") {
    out = { ...out, deviceAway: null };
  }
  return out !== v && movesMachine(ev) ? { ...out, machineVersion: out.machineVersion + 1 } : out;
}

type Presence = Extract<TaskEvent["type"], `presence.${string}`>;

/** Who is on the task is state about its viewers, not the thread - the presence
 *  fold (lib/presence.ts) takes these off the stream before they get here
 *  (hooks/use-task-stream.ts); a straggler that does reach the reducer changes nothing. The
 *  record type makes the list complete: a new presence type fails the build until it is named. */
const PRESENCE = {
  "presence.join": true,
  "presence.leave": true,
  "presence.here": true,
  "presence.typing": true,
} satisfies Record<Presence, true>;

function isPresence(ev: TaskEvent): ev is Ev<Presence> {
  return Object.hasOwn(PRESENCE, ev.type);
}

/** Every event type but presence, to its family's reducer. The table's type demands each one, so
 *  a new event type fails the build until a family folds it. */
const FOLDS: FoldTable<Exclude<TaskEvent["type"], Presence>> = {
  ...TASK_FOLDS,
  ...PLAN_FOLDS,
  ...MESSAGE_FOLDS,
  ...GATE_FOLDS,
  ...ARTIFACT_FOLDS,
  ...USAGE_FOLDS,
  ...STEER_FOLDS,
  ...MACHINE_FOLDS,
};

/** Fold one event into the view. Returns the same reference when a durable frame is a replayed
 *  duplicate (seq already applied), so React can skip a render. */
function reduceEvent(v: TaskView, ev: TaskEvent): TaskView {
  if (isPresence(ev)) return v;
  // Dedup durable frames by seq. Lane-2 (ephemeral) frames have no seq and always apply.
  const ephemeral = EPHEMERAL.has(ev.type);
  if (!ephemeral && ev.seq != null && ev.seq <= v.appliedSeq) return v;
  const appliedSeq = !ephemeral && ev.seq != null ? Math.max(v.appliedSeq, ev.seq) : v.appliedSeq;
  // A type the table lacks can only be a frame newer than this build: fail loudly rather than drop it.
  if (!Object.hasOwn(FOLDS, ev.type)) return assertNever(ev as never);
  const fold = FOLDS[ev.type] as Fold<typeof ev.type>;
  return fold({ ...v, appliedSeq }, ev);
}

function assertNever(ev: never): never {
  throw new Error(`unhandled TaskEvent: ${JSON.stringify(ev)}`);
}

/** "Not now" on a proposal card: local, the card stays as a quiet line. */
export function declineProposal(v: TaskView, id: string): TaskView {
  return {
    ...v,
    feed: v.feed.map((f) => (f.kind === "proposal" && f.id === id ? { ...f, open: false } : f)),
  };
}

/** The route in the user's words (the disclosure line on the epilogue). */
export function routeLabel(r: TurnRoute): string {
  // Said on the line itself, not only on hover: the person must not read "planned" for a turn
  // the planner never planned.
  if (r.fallback) return "planner failed, one step";
  if (r.source === "hatch") return "answered, then planned";
  if (r.lane === "express") return r.source === "anchor" ? "answered, unsure first" : "answered";
  if (r.lane === "direct") return "your bot did the work";
  const names: Record<string, string> = {
    standard: "planned",
    longform: "planned in sections",
    scatter: "planned wide",
    fleet: "every model answered",
    action: "acted",
    navigate: "navigated",
  };
  return names[r.lane] ?? r.lane;
}
