/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Gates' folds: a gate opening (the dock's question, the tree's wait row, or a pause no one
// answers) and resolving (the waited time, whose hand, the Q&A card), plus the rules every
// surface asks about an open gate: which dock face it wears and which scopes a viewer may pick.

import { deviceAwayLifted, deviceAwayOpened, pauseSummary } from "~/lib/device-away";
import { personName } from "~/lib/format";
import { answerLabel } from "./verdict-copy";
import {
  mergeToolRow,
  planGroupSeed,
  stepGroupSeed,
  upsertGroup,
  withExec,
} from "./task-view-exec";
import { withControl } from "./task-view-feed";
import type {
  Ev,
  ExecGroup,
  ExecRow,
  FeedItem,
  FoldTable,
  GateView,
  TaskView,
} from "./task-view-types";

type ToolRow = Extract<ExecRow, { t: "tool" }>;

/** Which face the dock wears for an open gate: the connect block, the Allow/Deny
 *  approval, the budget dock (B3: a "Budget reached" gate that names its ceiling), the control
 *  pointer at the screen, the numbered questions, or NONE. A gate with no questions
 *  and no verbs of its own (a cost confirmation, which carries no budget_scope) has
 *  nothing a question stepper can render: mounting one indexed an empty list and took the whole
 *  task page down for every editor. Such a gate waits
 *  on the header's controls and the CLI; the composer's text then steers instead of vanishing into
 *  a dock that is not there.
 *  A PAUSE gate is null: it is no one's to answer - the task's resume lifts it - so it
 *  draws no dock and no card, and the reducer keeps it out of `gates` altogether.
 *  `GateDock` is the one component that mounts by it - every host answers a gate through it. */
export function dockFor(
  gate: Pick<GateView, "kind" | "questions"> & { budget_scope?: string },
): "connect" | "approve" | "budget" | "control" | "questions" | "none" | null {
  if (gate.kind === "pause") return null;
  if (gate.kind === "connect") return "connect";
  if (gate.kind === "approve") return "approve";
  if (gate.kind === "budget" && gate.budget_scope) return "budget";
  // A person at the browser: no Allow, no stepper - the dock points at the screen
  // (the control dock draws it); the gate resolves when control is handed back.
  if (gate.kind === "control") return "control";
  return gate.questions.length > 0 ? "questions" : "none";
}

/** The open gates as THIS viewer may answer them - the web twin of the server's `gate_out`
 *  (`api/serializers/gates.py`). `always` writes a standing allowance on the creator's own
 *  account, so `gates.resolve` takes it from the task's creator alone (a Project editor still
 *  chooses once or task). `gate.opened` carries the full scope list to every viewer, so every
 *  surface that folds the stream (the task page's dock, the inbox's card) asks here and never
 *  offers a choice the resolve would refuse. A viewer the record cannot place is no creator. */
export function gatesFor(
  view: Pick<TaskView, "gates" | "creatorId">,
  viewerId: string | undefined,
): GateView[] {
  if (viewerId !== undefined && viewerId === view.creatorId) return view.gates;
  return view.gates.map((g) =>
    g.scopes?.includes("always") ? { ...g, scopes: g.scopes.filter((s) => s !== "always") } : g,
  );
}

/** What a gate's resolution reads as, by whose hand and what kind of gate it was. A CANCEL
 *  says nothing of its own: it is only ever the task's cancel closing its gates (or the
 *  clock), and the cancel line already told it. */
function gateLine(
  who: string,
  action: string | undefined,
  kind: string | undefined,
): string | null {
  if (action === "accept")
    // A person accepts a CONTROL gate only on a device: their Done hands the window back.
    return `${who} ${kind === "approve" ? "approved" : kind === "connect" ? "connected a service" : kind === "control" ? "handed back the browser" : "answered"}`;
  if (action === "decline")
    return `${who} ${kind === "approve" ? "declined" : kind === "connect" ? "skipped connecting" : kind === "control" ? "skipped" : "skipped the questions"}`;
  return null;
}

/** Was this gate a pause? A pause gate never enters `gates` (it is no one's to answer): the
 *  view keeps the ids it saw open, and the tree's still row backs that up. */
function isPauseGate(
  v: Pick<TaskView, "feed" | "pauseGates">,
  gateId: string | undefined,
): boolean {
  if (gateId != null && Object.hasOwn(v.pauseGates, gateId)) return true;
  const id = `gate-${gateId}`;
  return v.feed.some(
    (f) =>
      f.kind === "execution" &&
      f.groups.some((g) => g.rows.some((r) => r.t === "tool" && r.id === id && r.tool === "pause")),
  );
}

/** Rewrite the group holding the gate's tree row with `settle`, in whichever phase tree that
 *  row stands: a wait may have opened in an earlier tree than the frontier. */
function settleGateRow(
  feed: FeedItem[],
  gateId: string | undefined,
  settle: (g: ExecGroup, i: number, row: ToolRow) => ExecGroup,
): FeedItem[] {
  const id = `gate-${gateId}`;
  return feed.map((item) =>
    item.kind !== "execution"
      ? item
      : {
          ...item,
          groups: item.groups.map((g) => {
            const i = g.rows.findIndex((r) => r.t === "tool" && r.id === id);
            return i === -1 ? g : settle(g, i, g.rows[i] as ToolRow);
          }),
        },
  );
}

/** How long the row waited, opened to `at`: never negative, zero when the opening is unknown. */
function waitedMs(row: ToolRow, at: string): number {
  const waited = row.startedAt != null ? Date.parse(at) - Date.parse(row.startedAt) : 0;
  return Number.isFinite(waited) && waited > 0 ? waited : 0;
}

/** Settle a pause's row with the time the task sat paused (the resume lifted it). */
function settlePauseRow(feed: FeedItem[], gateId: string | undefined, at: string): FeedItem[] {
  return settleGateRow(feed, gateId, (g, i, row) => {
    const rows = g.rows.slice();
    rows[i] = { ...row, durationMs: waitedMs(row, at) };
    return { ...g, rows };
  });
}

type GateOpened = Ev<"gate.opened">;
type GateResolved = Ev<"gate.resolved">;

/** The group a gate's row joins: its step, else the planning group (gates may open before any
 *  step runs, while the task is still clarifying). */
function gateGroupSeed(ev: GateOpened): () => ExecGroup {
  return ev.step_id ? stepGroupSeed(ev.step_id, {}) : planGroupSeed("running", undefined, ev.at);
}

/** A mid-step pause parks its step on a gate no one answers: the task's resume lifts
 *  it. It joins no `gates` (no dock, no card, never "waiting on you") and the task stays paused;
 *  the tree keeps one quiet, still row - a pause is not work. */
function pauseGateOpened(v: TaskView, ev: GateOpened): TaskView {
  const gid = ev.step_id ?? "plan";
  return {
    ...v,
    pauseGates: { ...v.pauseGates, [ev.gate_id]: true },
    deviceAway: deviceAwayOpened(v.deviceAway, ev),
    ...withExec(v, gid, (item) =>
      upsertGroup(
        item,
        gid,
        (g) =>
          mergeToolRow(g, `gate-${ev.gate_id}`, {
            tool: "pause",
            summary: pauseSummary(ev),
            pending: false,
            ok: true,
            startedAt: ev.at,
          }),
        gateGroupSeed(ev),
      ),
    ),
  };
}

/** The open gate as the dock keeps it. */
function openGateView(v: TaskView, ev: GateOpened): GateView {
  const set = ev.questions;
  return {
    gate_id: ev.gate_id,
    title: ev.title ?? set?.title,
    kind: ev.kind,
    reason: ev.reason,
    connector_slug: ev.connector_slug,
    source: ev.source,
    host: ev.host,
    scopes: ev.scopes,
    asks_because: ev.asks_because,
    budget_scope: ev.budget_scope,
    budget_figures: ev.budget_figures,
    expires_at: ev.expires_at ?? set?.expires_at,
    questions: set?.questions ?? [],
    feedAt: v.feed.length,
  };
}

/** The tree's wait row while the gate is open, in the words of what it waits on. */
function gateWaitSummary(ev: GateOpened): string {
  const n = ev.questions?.questions?.length ?? 0;
  if (ev.kind === "approve") return `Waiting on your approval - ${ev.title ?? "an action"}`;
  if (ev.kind === "connect") return `Waiting on you - ${ev.title ?? "a connection"}`;
  if (ev.kind === "budget" && ev.reason) return `Waiting on a budget - ${ev.reason}`;
  if (n > 0) return `Waiting on you - ${n} question${n === 1 ? "" : "s"}`;
  return `Waiting on you - ${ev.title ?? "a decision"}`;
}

function gateOpened(v: TaskView, ev: GateOpened): TaskView {
  if (ev.kind === "pause") return pauseGateOpened(v, ev);
  const gates = [...v.gates.filter((g) => g.gate_id !== ev.gate_id), openGateView(v, ev)];
  // The tree's wait row: human time is task time too. Pending until gate.resolved
  // settles it with the waited duration - the honest "why did this step take 3m".
  const gid = ev.step_id ?? "plan";
  const withRow = withExec(v, gid, (item) =>
    upsertGroup(
      item,
      gid,
      (g) =>
        mergeToolRow(g, `gate-${ev.gate_id}`, {
          tool: "gate",
          summary: gateWaitSummary(ev),
          pending: true,
          startedAt: ev.at,
        }),
      gateGroupSeed(ev),
    ),
  );
  // NO top-level marker (four intake rounds stacked eight loose amber
  // lines over a settled answer). The tree's wait row is the record - folded with
  // everything else - and the live question's surface is the QuestionDock.
  // A PAUSED task stays paused when a step parks on a question (the server
  // sends no status frame) - flipping it to waiting would lose the pause and its Resume.
  return {
    ...v,
    ...withRow,
    status: v.status === "paused" ? "paused" : "waiting",
    gates,
  };
}

/** A steer that overtook an approval closes it as a decline naming the steer:
 *  the record says superseded, by whose steer - never "declined". */
function supersededLine(ev: GateResolved): string | null {
  if (typeof ev.content?.superseded_by !== "string") return null;
  return ev.actor ? `Superseded by ${personName(ev.actor.name)}'s steer` : "Superseded by a steer";
}

/** "Skipped 2 questions - proceeding with defaults", or without a count when none is known. */
function skippedSummary(n: number): string {
  return `Skipped ${n || ""}${n ? ` question${n === 1 ? "" : "s"}` : ""} - proceeding with defaults`.replace(
    "  ",
    " ",
  );
}

/** The settled wait row's words, by the kind of gate and what the person did. */
function waitSummary(
  ev: GateResolved,
  gateKind: string | undefined,
  shared: boolean,
  skipped: number,
): string {
  // Whose hand: on a shared task the record's person, never "you".
  const who = shared && ev.actor ? personName(ev.actor.name) : "You";
  if (gateKind === "connect") {
    if (ev.action === "accept") return "Connected - continuing";
    return ev.action === "decline" ? "Skipped connecting - continuing without" : "Cancelled";
  }
  if (gateKind === "approve") {
    if (ev.action === "accept") return `${who} allowed it`;
    return ev.action === "decline" ? `${who} denied it - continuing without` : "Cancelled";
  }
  if (ev.action === "accept") return `${who} answered`;
  return ev.action === "decline" ? skippedSummary(skipped) : "Cancelled";
}

/** The questions the person did not answer: absent, or filled from their default (`defaulted`;
 *  unrecorded on older events and the clock's, where a decline's content was all defaults). */
function isSkipped(ev: GateResolved): (id: string) => boolean {
  if (ev.action !== "accept" && ev.action !== "decline") return () => false;
  const content = ev.content ?? {};
  const defaulted = new Set(ev.defaulted ?? (ev.action === "decline" ? Object.keys(content) : []));
  return (id) => content[id] === undefined || defaulted.has(id);
}

/** Settle the tree's wait row with the WAITED duration (opened→resolved) - parked
 *  human time, named, so a 3-minute step honestly reads "waited on you 2m 40s". The
 *  skipped questions themselves stay as a quiet note: "Skipped - proceeding with
 *  defaults" that names nothing is a shrug. */
function settleWait(
  v: TaskView,
  ev: GateResolved,
  gate: GateView | undefined,
  supersededBy: string | null,
): FeedItem[] {
  if (v.execId == null) return v.feed;
  // What was actually skipped - the prompts, from the gate still in view state.
  const skippedId = isSkipped(ev);
  const skipped = (gate?.questions ?? []).filter((q) => skippedId(q.id)).map((q) => q.prompt);
  const summary = supersededBy ?? waitSummary(ev, gate?.kind, v.shared, skipped.length);
  return settleGateRow(v.feed, ev.gate_id, (g, i, row) => {
    const rows = g.rows.slice();
    rows[i] = {
      ...row,
      pending: false,
      ok: true,
      durationMs: waitedMs(row, ev.at),
      summary,
    };
    if (skipped.length > 0) {
      rows.splice(i + 1, 0, {
        t: "note",
        id: `skip-${ev.gate_id}`,
        tone: "muted",
        text: `Skipped: ${skipped.join(" · ")}`.slice(0, 300),
      });
    }
    return { ...g, rows };
  });
}

/** The person's own answers become the thread's record: the Q&A card - never a default
 *  a skip filled in. The gate is still in `v.gates` here, which is exactly why the card
 *  can pair prompts with answers - capture before the filter forgets the questions. */
function answeredItems(
  ev: GateResolved,
  gate: GateView | undefined,
): { prompt: string; answer: string }[] {
  const content = ev.content;
  if (!content || !gate) return [];
  const skipped = isSkipped(ev);
  return gate.questions
    .filter((q) => !skipped(q.id))
    .map((q) => ({ prompt: q.prompt, answer: answerLabel(q, content[q.id]) }));
}

function gateResolved(v: TaskView, ev: GateResolved): TaskView {
  if (isPauseGate(v, ev.gate_id)) {
    // The resume lifted a pause: its row keeps the paused time, and nothing
    // else - "Bob resumed" is the status frame's line, never "Bob answered".
    return {
      ...v,
      feed: settlePauseRow(v.feed, ev.gate_id, ev.at),
      deviceAway: deviceAwayLifted(v.deviceAway, ev.gate_id),
    };
  }
  const gate = v.gates.find((g) => g.gate_id === ev.gate_id);
  const supersededBy = supersededLine(ev);
  // Whose hand resolved it: a control line when the frame names a person;
  // a timeout or the clock names no one and adds nothing.
  const line = ev.actor
    ? (supersededBy ?? gateLine(personName(ev.actor.name), ev.action, gate?.kind))
    : null;
  // A decline leaves NO top-level marker (the loose-line stack): the
  // wait row + its skipped-prompts note inside the tree ARE the record, folded
  // with the rest of the machinery.
  const settled = withControl(
    {
      ...v,
      feed: settleWait(v, ev, gate, supersededBy),
      gates: v.gates.filter((g) => g.gate_id !== ev.gate_id),
    },
    line,
  );
  const items = answeredItems(ev, gate);
  if (items.length === 0) return settled;
  // "Cy answered", then the record of what was asked and said.
  return {
    ...settled,
    feed: [...settled.feed, { kind: "qa", id: `qa${settled.nextId}`, items }],
    nextId: settled.nextId + 1,
  };
}

export const GATE_FOLDS: FoldTable<"gate.opened" | "gate.resolved"> = {
  "gate.opened": gateOpened,
  "gate.resolved": gateResolved,
};
