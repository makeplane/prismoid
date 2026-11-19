/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The plan's folds: the plan opening the execution tree, its revisions and steers, each step's
// story as its state frames land, and the diagnosis card when a step gives up.

import type { StepState } from "~/lib/api/events";
import { ownsGroup, planGroupSeed, stepGroupSeed, upsertGroup, withExec } from "./task-view-exec";
import { authorOf, plainFinding, stripFinding, upsertStep } from "./task-view-feed";
import { ACCEPTED_LINE } from "./task-view-steer";
import type {
  Ev,
  ExecGroup,
  ExecRow,
  FeedItem,
  FoldTable,
  StepView,
  TaskView,
  TurnRoute,
} from "./task-view-types";

type StepFrame = Ev<"step.state">;

/** The rail's timeline reads v.steps - seed them from the plan TOO, not only the
 *  exec-tree checklist. A task watched from birth builds its steps purely from
 *  step.state frames, which carry agent/state but not `why`: without this
 *  merge the routing reason rendered only after a reload, which is exactly the kind
 *  of two-faced behaviour the live/durable reconcile law exists to prevent. */
function planSteps(steps: StepView[], ev: Ev<"task.planned">): StepView[] {
  let out = steps;
  for (const s of ev.plan?.steps ?? []) {
    out = upsertStep(out, {
      id: s.id,
      title: s.title,
      state: s.state,
      agent: s.agent,
      attempt: s.attempt ?? 0,
      why: s.why || undefined,
      botVersion: s.bot_version ?? undefined,
    });
  }
  return out;
}

function routeOf(ev: Ev<"task.planned">, prev: TurnRoute | null): TurnRoute | null {
  if (!ev.route) return prev;
  return {
    lane: ev.route,
    source: ev.route_source ?? "planner",
    why: ev.route_why ?? "",
    ...(ev.fallback ? { fallback: ev.fallback } : {}),
  };
}

/** Route sources from before the Lead: a planner drafted the plan, so it joins the cast. A plan the
 *  Lead made (`lead`) or a recipe cut (`dial`) has no planner, and the group names nobody. A
 *  frame with no source is one of the old rows (`routeOf` reads it as "planner" too). */
const PLANNER_SOURCES: ReadonlySet<string> = new Set(["planner", "detector", "anchor", "hatch"]);

function plannerOf(source: string | undefined): "planner" | undefined {
  return source === undefined || PLANNER_SOURCES.has(source) ? "planner" : undefined;
}

/** The plan opens the execution tree: a "plan" group whose checklist is the plan's
 *  own steps, ticking as step.state frames land. (The old "Plan ready" marker's
 *  one line said nothing; the tree says everything.) */
function taskPlanned(v: TaskView, ev: Ev<"task.planned">): TaskView {
  const checklist = (ev.plan?.steps ?? []).map((s) => ({
    id: s.id,
    title: s.title,
    state: s.state,
  }));
  return {
    ...v,
    steps: planSteps(v.steps, ev),
    route: routeOf(ev, v.route),
    ...withExec(v, "plan", (item) =>
      upsertGroup(
        item,
        "plan",
        (g) => ({
          ...g,
          state: "done" as StepState,
          agent: plannerOf(ev.route_source),
          checklist: checklist.length > 0 ? checklist : g.checklist,
        }),
        planGroupSeed("done", checklist, ev.at),
      ),
    ),
  };
}

/** A STEER edit is the user's turn: the backend records steers as
 *  plan.edited{kind:"steer"}. */
function planEdited(v: TaskView, ev: Ev<"plan.edited">): TaskView {
  if (ev.edit?.kind === "steer" && ev.edit.reason) {
    return steerBubble(v, ev, ev.edit.reason);
  }
  return planRevised(v, ev);
}

function steerBubble(v: TaskView, ev: Ev<"plan.edited">, reason: string): TaskView {
  const author = authorOf(ev.actor);
  // The steer is ACCEPTED the moment it lands (its acknowledgements join by
  // command id), and the epoch it opened is the one the composer stamps next.
  const commandId = ev.edit?.command_id ?? undefined;
  return {
    ...v,
    feed: [
      ...v.feed,
      {
        kind: "user",
        id: `u${v.nextId}`,
        text: reason,
        steered: true,
        ...(author ? { author } : {}),
        ...(commandId
          ? {
              commandId,
              ack: { state: "accepted" as const, line: ACCEPTED_LINE },
            }
          : {}),
      },
    ],
    nextId: v.nextId + 1,
    steerEpoch: Math.max(v.steerEpoch, ev.edit?.epoch ?? 0),
  };
}

/** Keep the superseded plan - a revision is history, not an erasure. Steps that
 *  survive the edit re-upsert into `steps` via their step.state replays. In the
 *  tree the revision is a quiet note on the plan group, not a top-level marker. */
/** Edits that ADD to the plan (the Lead handing over to its steps, a long-form fan-out, intake
 *  answers, a mid-run ask) are not a revision: nothing was superseded, so the rail keeps no
 *  "Earlier plans" entry for them. Only a re-plan (or a retired hatch) replaces steps. */
const JOINING_EDITS: ReadonlySet<string> = new Set(["lead", "expand", "clarify", "ask"]);

/** A plan that was replaced: a step it never finished is cut off, not still running. */
function superseded(steps: StepView[]): StepView[] {
  return steps.map((s) => (SETTLED.has(s.state) ? s : { ...s, state: "skipped" as StepState }));
}
const SETTLED: ReadonlySet<StepState> = new Set(["done", "escalated", "skipped"]);

function planRevised(v: TaskView, ev: Ev<"plan.edited">): TaskView {
  const noteId = `n${v.nextId}`;
  // A re-plan the system made (the boundary's, fence 2's) says whose steer
  // it served - "Re-planned for Bob's steer: '…'"; any other revision stays quiet.
  const text =
    ev.edit?.kind === "replan" && !ev.actor && ev.edit.reason ? ev.edit.reason : "Plan revised";
  const slices = withExec(v, "plan", (item) =>
    upsertGroup(
      item,
      "plan",
      (g) => ({
        ...g,
        rows: [...g.rows, { t: "note", id: noteId, tone: "info", text }],
      }),
      planGroupSeed("done", undefined, ev.at),
    ),
  );
  return {
    ...v,
    planHistory:
      v.steps.length > 0 && !JOINING_EDITS.has(ev.edit?.kind ?? "")
        ? [...v.planHistory, superseded(v.steps)]
        : v.planHistory,
    ...slices,
    nextId: slices.nextId + 1,
  };
}

/** A step's state that closes its clock. */
function endsStep(state: StepState): boolean {
  return state === "done" || state === "escalated" || state === "skipped";
}

/** A retry becomes a warning note INSIDE the group (intra-step events belong to the step). */
function retryRows(g: ExecGroup, ev: StepFrame, noteId: string): ExecRow[] {
  if (ev.state !== "retrying") return g.rows;
  const finding = stripFinding(ev.detail);
  return [
    ...g.rows,
    {
      t: "note",
      id: noteId,
      tone: "warning",
      text: `Attempt ${ev.attempt} didn't produce the deliverable - taking another pass${
        finding ? `: ${finding}` : ""
      }`,
    },
  ];
}

function updateStepGroup(
  g: ExecGroup,
  ev: StepFrame,
  botVersion: number | null | undefined,
  noteId: string,
): ExecGroup {
  return {
    ...g,
    title: ev.title || g.title,
    state: ev.state,
    agent: ev.agent ?? g.agent,
    attempt: ev.attempt,
    modelRef: ev.model_ref ?? g.modelRef,
    // The newest frame's word, absent included: a resumed step sheds "Blocked".
    outcome: ev.outcome ?? undefined,
    blockedBy: ev.blocked_by ?? undefined,
    botVersion: g.botVersion ?? botVersion,
    startedAt: g.startedAt ?? (ev.state === "running" ? ev.at : undefined),
    endedAt: endsStep(ev.state) ? ev.at : g.endedAt,
    rows: retryRows(g, ev, noteId),
  };
}

const ENDED = new Set(["cancelled", "failed", "delivered"]);
function stepState(v: TaskView, ev: StepFrame): TaskView {
  const stepId = ev.step_id ?? ev.title;
  const steps = upsertStep(v.steps, {
    id: stepId,
    title: ev.title,
    state: ev.state,
    agent: ev.agent,
    attempt: ev.attempt,
    detail: ev.detail,
  });
  // The tree carries the step's whole story: the group seeds/updates, the plan
  // group's checklist ticks, timing anchors land, and a retry becomes a warning
  // note INSIDE the group (the old top-level marker is gone - intra-step events
  // belong to the step). `escalated` stays quiet here: the `escalation.raised`
  // card beside it carries the full diagnosis.
  const noteId = `n${v.nextId}`;
  // Two homes under the phase law: the checklist tick goes to the tree that OWNS the
  // plan group; the step's own story goes to the tree that owns the step (minting a
  // new phase tree when the step is new and content has landed since).
  const ticked = withExec(v, "plan", (item) =>
    upsertGroup(
      item,
      "plan",
      (g) => ({
        ...g,
        checklist: g.checklist?.map((c) => (c.id === stepId ? { ...c, state: ev.state } : c)),
      }),
      planGroupSeed("done", undefined, ev.at),
    ),
  );
  // A step an ended turn marks skipped before it ever ran (the steps a stop never reached) has no
  // story of its own: the plan's checklist ticks it and the stop's account names it, and it opens
  // no tree under the turn's end.
  if (ev.state === "skipped" && ENDED.has(v.status) && !ownsGroup(v, stepId)) {
    return { ...v, steps, ...ticked };
  }
  const botVersion = steps.find((s) => s.id === stepId)?.botVersion;
  const slices = withExec(
    { ...v, ...ticked },
    stepId,
    (item) =>
      upsertGroup(
        item,
        stepId,
        (g) => updateStepGroup(g, ev, botVersion, noteId),
        stepGroupSeed(stepId, ev),
      ),
    { phase: true },
  );
  return {
    ...v,
    steps,
    ...slices,
    nextId: ev.state === "retrying" ? slices.nextId + 1 : slices.nextId,
  };
}

const EVIDENCE_SHOWN = 3;

/** A rung's stop line leads with its label (`could not be done.`, `blocked (…).`). */
const STOP_LABEL = /^(could not be done|blocked \([^)]*\))\.\s*/i;
/** A steer's re-run brief: written to the agent ("redo it in /work"), never a finding. */
const STEER_BRIEF = /\*\*[^*]+ steered\*\*/;

/** The attempts' own findings, made legible, each distinct one once (three retries that hit the
 *  same wall report it three times) and at most a few of them. The evidence is the retries' brief:
 *  a steer's brief and the line that only repeats the card's reason are not shown. */
function evidenceOf(evidence: readonly unknown[] | undefined, reason: string): string[] {
  const seen = new Set<string>();
  for (const line of evidence ?? []) {
    if (STEER_BRIEF.test(String(line))) continue;
    const finding = plainFinding(String(line)).replace(STOP_LABEL, "").trim();
    if (finding && !(reason && reason.endsWith(finding))) seen.add(finding);
  }
  return [...seen].slice(0, EVIDENCE_SHOWN);
}

/** The full diagnosis, as a card the reader can act on. The old one-line marker took
 *  `why`'s FIRST line - which for a produce failure is the preamble ("3 attempts, each
 *  one failed. The last, because:") - so the page showed a colon and no reason at all
 *  (the consciousness-theory task). Headline, the actual reason, what to try. */
function escalationRaised(v: TaskView, ev: Ev<"escalation.raised">): TaskView {
  const d = ev.diagnosis;
  const why = String(d?.why ?? "");
  const afterColon = why.includes(":\n") ? why.slice(why.indexOf(":\n") + 2) : why;
  const reason = plainFinding(afterColon);
  const item: FeedItem = {
    kind: "escalation",
    id: `e${v.nextId}`,
    headline: d?.what_failed ?? "A step could not finish",
    reason: reason || "No reason was recorded.",
    evidence: evidenceOf(d?.evidence, reason),
    tries: (d?.suggested_next ?? []).map(String),
  };
  return { ...v, feed: [...v.feed, item], nextId: v.nextId + 1 };
}

/** Pure timing marks for the hop ledger (`just range`); the thread renders
 *  nothing - the record is their whole audience. */
function stepDequeued(v: TaskView): TaskView {
  return v;
}

/** The assembled prompt, on the record for the bot author's "why did it do
 *  that". A debugging receipt, not conversation - the thread never renders it; a prompt
 *  inspector is the named web follow-up. Replay must never crash on it. */
function stepPrompt(v: TaskView): TaskView {
  return v;
}

export const PLAN_FOLDS: FoldTable<
  | "task.planned"
  | "plan.edited"
  | "step.state"
  | "escalation.raised"
  | "step.dequeued"
  | "step.prompt"
> = {
  "task.planned": taskPlanned,
  "plan.edited": planEdited,
  "step.state": stepState,
  "escalation.raised": escalationRaised,
  "step.dequeued": stepDequeued,
  "step.prompt": stepPrompt,
};
