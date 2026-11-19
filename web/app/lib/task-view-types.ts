/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The task view's shapes: what the fold in task-view.ts builds and every surface reads. Kept in
// a leaf module so the per-family reducers can share them without importing the facade.

import type {
  BudgetFigures,
  ContextLaneView,
  Question,
  ScreenBox,
  StepState,
  TaskEvent,
  TaskStatus,
  Verdict,
  TaskEnding,
} from "~/lib/api/events";
import type { DeviceWait } from "~/lib/device-away";
import type { UsageLedger } from "./usage-ledger";

/** Who drives the Machine's browser, as the page keeps it. */
export type ScreenLease = {
  holder: { id: string; name: string };
  private: boolean;
  until: string | null;
};

export type FeedTone = "info" | "success" | "warning" | "danger" | "muted";

export type FeedItem =
  | {
      kind: "user";
      id: string;
      text: string;
      opening?: boolean;
      /** Painted optimistically by the composer, before the wire answers; the durable
       *  `task.continued` frame reconciles it in place. */
      pending?: boolean;
      /** WHO sent it, off the event's actor. Absent on the optimistic paint
       *  (that row is yours) and on every event that names nobody. */
      author?: { id: string; name: string };
      /** A steer (plan.edited) rather than an ask - the name line says "steered". */
      steered?: boolean;
      /** A steer's command id: what its acknowledgements and its Retract name. */
      commandId?: string;
      /** Where the steer stands, in one line under the bubble. */
      ack?: SteerAck;
    }
  | {
      /** A human's action on the task as one muted line - "Ada paused", "Bob resumed", "Cy
       *  answered". Minted only from events that NAME an actor, so a task no one
       *  else touched reads as it always did. */
      kind: "control";
      id: string;
      text: string;
    }
  | {
      /** The answered question batch, as the thread keeps it: what was asked, what you
       *  said - minted when a gate resolves with answers. */
      kind: "qa";
      id: string;
      items: { prompt: string; answer: string }[];
    }
  | { kind: "artifact"; id: string; artifactId: string; title?: string }
  | {
      /** Working-role artifacts as a compact CHIP ROW (round six): section drafts,
       *  finals, outlines - honest and clickable, never full-card peers of the real
       *  deliverable (a 17-page run stacked ~14 identical cards). Consecutive working
       *  publishes fold into one row. */
      kind: "working-artifacts";
      id: string;
      items: { artifactId: string; title: string; type?: string }[];
    }
  | {
      kind: "segment";
      id: string;
      agent: string;
      markdown: string;
      live: boolean;
    }
  | {
      kind: "marker";
      id: string;
      tone: FeedTone;
      glyph: MarkerGlyph;
      text: string;
    }
  | {
      /** The full diagnosis when a step gave up (`escalation.raised`) - a card, not a one-line
       *  marker, because the one line used to cut the reason off mid-sentence ("The last,
       *  because:") and leave the reader with jargon. Headline + the actual reason + what to
       *  try, in human words. */
      kind: "escalation";
      id: string;
      headline: string;
      reason: string;
      /** What the attempts themselves reported (`diagnosis.evidence`): the same finding once,
       *  never one copy per retry. The generic reason says WHAT happened; this says WHY. */
      evidence: string[];
      tries: string[];
      /** The task then failed `could_not`: an honest could-not, framed neutral, never as a fault. */
      couldNot?: boolean;
    }
  | {
      /** The answerer's soft hatch: a proposal card under a reply. Start is a
       *  follow-up with the deliverable dial set; Not now is nothing. `open` flips off
       *  when the reader declines or the next turn opens - a card never outlives its turn. */
      kind: "proposal";
      id: string;
      goal: string;
      deliverable: string | null;
      why: string;
      turn: number;
      open: boolean;
    }
  | {
      /** The end-of-turn story, consolidated (three stacked markers - green
       *  Delivered, red Verification, Remembered - read as a traffic light; one quiet
       *  block reads as a sentence). Verification and memory fold IN as they arrive. */
      kind: "epilogue";
      id: string;
      delivered: boolean;
      /** `partial`: the answer says it did only part of the work - the marker says so, never "Delivered". */
      partial?: boolean;
      /** `could_not`: the answer turned the request down or said it cannot be done - never "Delivered". */
      couldNot?: boolean;
      /** A stopped turn (cancelled, or stopped at its budget): why, who, and the account of what it
       *  finished and never did - the end of the turn says so instead of falling silent. */
      stopped?: { by: string | null; reason: string; account: string };
      /** The routed record for the turn: which lane answered and why - the
       *  disclosure that makes "why did this become a document?" legible at a glance. */
      route?: TurnRoute;
      verdict?: Verdict;
      suite?: string;
      family?: string | null;
      /** The artifact the receipt is about - the caveat on a failed receipt opens it. */
      artifactId?: string;
      /** What checking the answer cost (the receipt's own spend, landed after delivery). */
      checkUsd?: number;
      remembered: { kind?: string; text: string }[];
      /** The TURN's conclusion (the phase split solved the
       *  breakdown and orphaned the total - each tree wears its slice, nobody said what
       *  the whole turn took). Raw values summed across the turn's phase trees at
       *  delivery; renderers format. Absent parts stay absent - a legacy replay with no
       *  anchors shows no receipt rather than a guess (the round-four doctrine).
       *  `rows` is the x-ray behind the numbers (the hover ask): every group of the
       *  turn's trees in feed order, the plan phase included - the cast rollup needs
       *  its planner. */
      receipt?: {
        steps: number;
        agents: number;
        durationMs?: number;
        tokens?: number;
        /** Cache-read and cache-write tokens - the ones that explain the cost. */
        cachedTokens?: number;
        rows?: {
          title: string;
          agent?: string | null;
          kind: "plan" | "step";
          state: StepState;
          durationMs?: number;
          tokens?: number;
          cachedTokens?: number;
        }[];
      };
    }
  | {
      /** The execution story of ONE turn - the progress tree. All machinery folds in here
       *  (plan checklist, step groups, tool rows, retry notes); all prose stays top-level.
       *  Settled tasks render it as one quiet "Completed N steps" line. One item = one
       *  MessageScrollerItem = one content-visibility boundary. */
      kind: "execution";
      id: string;
      turn: number;
      settled: boolean;
      groups: ExecGroup[];
    };

/** A steer's acknowledgement: accepted the moment it lands, then applied at a safe
 *  point, rejected when the task ended first, or retracted by its author. */
export type SteerAck = {
  state: "accepted" | "applied" | "rejected" | "retracted";
  line: string;
};

/** Marker glyphs. Minted today: `verified`/`failed` (a fleet seat's mid-run receipt has
 *  no epilogue) and `memory` (epilogue-less replays). The rest are vestigial - the tree
 *  and the epilogue absorbed their mint sites - but stay in the union so the glyph map
 *  keeps rendering any older serialized surface that still names them. */
export type MarkerGlyph =
  | "plan"
  | "step"
  | "retry"
  | "tool"
  | "artifact"
  | "verified"
  | "failed"
  | "escalation"
  | "gate"
  | "delivered"
  | "clarify"
  | "memory"
  | "machine";

export type StepView = {
  id: string;
  title: string;
  state: StepState;
  agent?: string | null;
  attempt: number;
  detail?: string;
  /** One's reason for choosing this agent - empty for deterministic lanes. */
  why?: string;
  /** The bot version this step was pinned to at plan time. */
  botVersion?: number | null;
};

/** One row inside an execution group - a tool call (live-pending until its end/durable
 *  frame merges in) or a note (retry finding, plan revision). Row ids are the reconcile
 *  law: `tool_call_id` when the wire carries one, so the live `activity` start and the
 *  durable `tool.called` land on the SAME row in both lanes. */
export type ExecRow =
  | {
      t: "tool";
      id: string;
      tool: string;
      summary?: string;
      ok?: boolean;
      pending?: boolean;
      durationMs?: number;
      deniedReason?: string;
      /** When the row opened (gate waits: opened→resolved spans minutes of human time). */
      startedAt?: string;
      /** A Machine phase row: the server's bound (seconds) on the wait it stands for. */
      waitS?: number;
    }
  | {
      /** A model round: "Thinking…" while open, "Thought for Ns" when the first visible
       *  part arrives. `thinkingText` streams in live only (ephemeral, absent on replay -
       *  the same sanctioned delta as `segment.live`). */
      t: "round";
      id: string;
      pending?: boolean;
      durationMs?: number;
      thinkingText?: string;
    }
  | { t: "note"; id: string; tone: FeedTone; text: string };

/** One group in the execution tree - a plan-phase group or one step. A group is a FROZEN
 *  snapshot: it carries its own state/title/timing and is never re-derived from `v.steps`
 *  (which resets on `task.continued`). */
export type ExecGroup = {
  id: string; // step_id, or "plan" for the synthetic planning group
  kind: "plan" | "step";
  title: string;
  state: StepState;
  agent?: string | null;
  attempt: number;
  modelRef?: string;
  /** A guarded step's typed ending, as its newest step.state frame says:
   *  "partial" on a done step, "blocked" (with `blockedBy`) on an escalated or parked one. */
  outcome?: "done" | "partial" | "blocked" | "failed";
  blockedBy?: "login" | "approval" | "person" | "budget";
  /** The bot version the step's pin resolved to - the tree says "reviewer v3". */
  botVersion?: number | null;
  acceptance?: string[];
  /** The step's own ledger, folded from usage.recorded (units + cost) - "what did this
   *  step cost" answered in place. */
  calls?: number;
  /** Web searches, counted apart from the model calls above. */
  searches?: number;
  costUsd?: number;
  tokens?: number;
  /** Cache-read and cache-write tokens, apart from `tokens` (fresh input and output). */
  cachedTokens?: number;
  /** Every model the step's ledger rows used, in first-use order. */
  models?: string[];
  startedAt?: string;
  endedAt?: string;
  /** Plan group only: the plan's own steps, ticking as step.state frames land. */
  checklist?: { id: string; title: string; state: StepState }[];
  rows: ExecRow[];
};
export type ArtifactView = {
  id: string;
  title?: string;
  type?: string;
  verdict?: Verdict | null;
  revision?: number; // >1 ⇒ the lineage has been edited (artifact.revised)
  role?: string; // deliverable | working - the reader's auto-open skips working images
};
export type GateView = {
  gate_id: string;
  title?: string;
  /** approve | input | budget - the dock picks its face by this. */
  kind?: string;
  /** An approve gate's evidence blocks, a connect gate's why; empty elsewhere. */
  reason?: string;
  /** A connect gate's service slug - what the block offers. */
  connector_slug?: string;
  /** An approve gate's origin: "connector" (runs on the task creator's connection, so only
   *  they may allow it) or "browser" (any editor may allow it once or for the task). */
  source?: string;
  /** An approve gate's host, browser-sourced - empty for a
   *  connector gate. */
  host?: string;
  /** The scopes a browser approve gate offers ("once" | "task" | "always"); empty for a
   *  connector gate or a browser danger ask, which keeps the plain Allow/Deny face. */
  scopes?: string[];
  /** APPROVE gates: why the approval rules asked ("it moves money"), each once - said first. */
  asks_because?: string[];
  /** BUDGET gates (B3): which ceiling a "Budget reached" gate hit - "task" or "project", or
   *  "workspace" for a "Spending allowance reached" gate; empty for the long-form cost
   *  confirmation. */
  budget_scope?: string;
  /** BUDGET gates: the ceiling, its spend and the coming wave - what the dock's raise must clear. */
  budget_figures?: BudgetFigures | null;
  expires_at?: string;
  /** "device" when the task's browser is a window on the user's own computer:
   *  a CONTROL gate there is ended with Done, the creator's accept - no screen, no
   *  takeover. The task page sets it from its Machine; `GateOut` carries it. */
  machine_driver?: string | null;
  questions: Question[];
  /** The feed's length when the gate opened: a feed item at or past it came after the
   *  question (the dock's "Bob steered while this waited"). */
  feedAt?: number;
};
export type Verification = {
  verdict: Verdict;
  verifier_family?: string | null;
  artifact_id?: string;
  suite?: string;
  signature?: string;
  /** Spend that landed after delivery on the producing step, before this receipt signed: the
   *  cost of checking the answer (judge, claim reader). Folded off the stream; absent when
   *  nothing was spent or the receipt came before delivery. */
  costUsd?: number;
};

/** The turn's route, off `task.planned`. */
/** `fallback`: the planner failed and its one-step floor ran - why (absent otherwise). */
export type TurnRoute = { lane: string; source: string; why: string; fallback?: string };

export type TaskView = {
  status: TaskStatus;
  /** The settled turn's typed ending, off its settle frame: `could_not` reads apart from a fault. */
  ending: TaskEnding | null;
  goal?: string;
  /** The minted (or user-set) label, live off the stream - list chrome reads it. */
  title?: string;
  steps: StepView[];
  /** Superseded step sets, oldest first - snapshotted when a `plan.edited` frame lands, so a
   *  revision never erases what the plan used to be. */
  planHistory: StepView[][];
  feed: FeedItem[];
  /** The live turn's execution item id - found in `feed` by id scan from the tail (never
   *  an index: inserts and filters would stale it). Null until the first machinery frame
   *  of a turn; nulled again when the turn settles. */
  execId: string | null;
  /** The live turn's epilogue item id - minted at delivery; verification and memory fold
   *  into it; reset when the next turn opens. */
  epilogueId: string | null;
  artifacts: ArtifactView[];
  verification: Verification | null;
  gates: GateView[];
  /** Model calls on the ledger - a web search is counted in `usageSearches`, never here. */
  usageCalls: number;
  usageSearches: number;
  /** The ledger folded by model, step and native unit, for the Models popover. */
  usageLedger: UsageLedger;
  totalUsd: string;
  /** Spend folded live off `usage.recorded` frames - null until the first one. The stream
   *  replays from seq 0, so once any frame lands this IS the full ledger sum; `totalUsd`
   *  (the loader snapshot) only covers the paint before replay arrives. The header was
   *  frozen at the load-time figure while calls counted up - "$0.0000 · 17 calls". */
  usageUsd: number | null;
  /** Spend that landed after delivery, by the step that spent it, waiting for its receipt to
   *  sign: when `verification.signed` names that step the amount becomes the receipt's own
   *  cost. A step's late spend with no receipt stays here, unlabelled and uncounted below. */
  lateUsd: Record<string, number>;
  /** The part of `usageUsd` that was verification (every receipt's `costUsd` summed). */
  verifyUsd: number;
  live: { phase: string; elapsed_s: number } | null;
  /** The document being composed right now (streamed out of write_file's args) -
   *  rendered as a growing draft card until artifact.produced replaces it with the real card. */
  draft: { path: string; text: string } | null;
  /** What this task was told beyond its ask (`context.assembled`) - lanes, token counts,
   *  truncations. Rail data, not a feed item; replay and live agree by construction. */
  context: { lanes: ContextLaneView[]; totalTokens: number } | null;
  appliedSeq: number;
  nextId: number;
  /** Bumped by every machine.* event: the route revalidates the Machine chip off it. */
  machineVersion: number;
  /** The Machine's image as last named by machine.created/woke (an upgrade wake differs). */
  machineProfile: string | null;
  /** The turn of the Machine's last created/woke event: a wake in a later turn is a follow-up's. */
  machineTurn: number | null;
  /** Where the agent last acted on the Machine's browser (`screen.action`): the pane
   *  draws `box` for 1.5 s from `at`. Lane 2: absent on replay, never part of the thread. */
  screenAction: { verb: string; box: ScreenBox | null; at: string } | null;
  /** Who drives the browser right now: seeded from the task's Machine, then `screen.state`.
   *  Null = nobody, the agent drives. State, not the record: no feed item. */
  screen: ScreenLease | null;
  /** Counts every `screen.state` (they carry no order): the page revalidates the task read on each
   *  and reseeds `screen` from `machine.control`, so a stale frame cannot stick. */
  screenVersion: number;
  /** The newest `machine.control_ended`: who held the browser (`holderId`, the
   *  envelope's actor; "" when the record names nobody), the Project owner who ended it (`endedBy`,
   *  else null), why and when - so the screen can tell the holder "{endedBy} ended your control." */
  controlEnded: {
    holderId: string;
    endedBy: { id: string; name: string } | null;
    reason: string;
    at: string;
  } | null;
  delivered: boolean;
  deliveredSummary?: string;
  parentTaskId?: string | null; // the task this one was FORKED from - renders the parent thread
  /** Who created the task - `task.created`'s actor (a User's id is its Actor's id).
   *  Unknown until that frame folds; `gatesFor` reads it. */
  creatorId?: string;
  /** The task lives in a Project: its tree names whoever resolved a gate, as its
   *  timeline does, instead of "you" - a teammate reads Ada's answer as Ada's. */
  shared: boolean;
  /** The conversation counter, 1-based - a follow-up bumps it in place. */
  turn: number;
  /** The current turn's thumbs - the newest `task.feedback` frame; resets when the
   *  next turn opens. */
  feedback: "up" | "down" | null;
  /** The live turn's route - the newest `task.planned` frame; a hatch re-plan
   *  overwrites it mid-turn (the record's own word); resets when the next turn opens. */
  route: TurnRoute | null;
  /** The newest plan epoch this screen has seen: the composer stamps it on a steer
   *  as `issued_epoch` when typing begins, and a steer against an older one is a 409. */
  steerEpoch: number;
  /** A person's pause, from the moment they asked (`applied: false` - "Pausing…") until it
   *  took hold (`pause.applied`). Null whenever the task is not paused, or no one is named. */
  pause: { by: string; applied: boolean } | null;
  /** Artifacts a steer's re-run superseded, each with whose steer it was (`steer.applied`'s
   *  author, else "a teammate"): the feed card greys and says so; the shelf and the inbox
   *  leave them out - never the delivered card. */
  superseded: Record<string, string>;
  /** Every PAUSE gate this view has seen open (`gate.opened{pause}`): a pause gate
   *  never enters `gates`, so its `gate.resolved` reads as a pause off this set, never as an
   *  answer - whether or not the tree still holds its row. */
  pauseGates: Record<string, true>;
  /** A step waiting for its device: the open device-away PAUSE gate
   *  (`device_away:` reason) and its title, "Waiting for MacBook Pro". The paused line says it
   *  instead of "Paused". Null once that gate is lifted or the task is no longer paused. */
  deviceAway: DeviceWait;
};

export type ExecutionItem = Extract<FeedItem, { kind: "execution" }>;
export type Segment = Extract<FeedItem, { kind: "segment" }>;
export type EpilogueItem = Extract<FeedItem, { kind: "epilogue" }>;
export type ReceiptRow = NonNullable<NonNullable<EpilogueItem["receipt"]>["rows"]>[number];

/** One event type's frame, narrowed by its `type`. */
export type Ev<K extends TaskEvent["type"]> = Extract<TaskEvent, { type: K }>;

/** A family reducer: folds one frame into a view whose `appliedSeq` already counts it. */
export type Fold<K extends TaskEvent["type"]> = (v: TaskView, ev: Ev<K>) => TaskView;

/** One reducer per event type of a family. The facade spreads every family into one table, and
 *  the table's type demands every event type, so a new one cannot go unhandled. */
export type FoldTable<K extends TaskEvent["type"]> = { [P in K]: Fold<P> };
