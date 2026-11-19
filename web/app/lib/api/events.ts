/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The TaskEvent wire union, as a clean discriminated union for the SSE reducer (F4).
//
// docs/api/events.schema.json is the frozen source of truth (49 variants, one oneOf on `type`), and
// `events.gen.ts` is generated from it - but json-schema-to-typescript emits loose, aliased
// discriminators that don't narrow well in a switch. This file is the hand-kept typed façade the reducer
// switches on; a `never`-exhaustiveness check there means a drift between this and the schema fails the
// build. Only fields the UI actually reads are modelled.

export type TaskEventType =
  | "task.created"
  | "task.clarifying"
  | "task.planned"
  | "task.status"
  | "task.dequeued"
  | "step.dequeued"
  | "task.moved"
  | "task.budget_set"
  | "machine.created"
  | "machine.slept"
  | "machine.woke"
  | "machine.rebuilt"
  | "machine.reset"
  | "machine.control_claimed"
  | "machine.control_ended"
  | "task.renamed"
  | "task.cancelled"
  | "task.delivered"
  | "task.continued"
  | "task.proposed"
  | "task.feedback"
  | "plan.edited"
  | "steer.applied"
  | "steer.rejected"
  | "steer.retracted"
  | "pause.applied"
  | "step.state"
  | "step.narration"
  | "step.prompt"
  | "tool.called"
  | "gate.opened"
  | "gate.resolved"
  | "usage.recorded"
  | "artifact.produced"
  | "artifact.revised"
  | "artifact.snapshot"
  | "verification.signed"
  | "qa.reviewed"
  | "escalation.raised"
  | "context.assembled"
  | "memory.written"
  | "bot_memory.written"
  | "message.start"
  | "message.delta"
  | "message.end"
  | "artifact.delta"
  | "progress"
  | "activity"
  | "presence.join"
  | "presence.leave"
  | "presence.here"
  | "presence.typing"
  | "screen.state"
  | "screen.action";

import type { components } from "./types.gen";

export type Question = components["schemas"]["Question"];
export type QuestionSet = components["schemas"]["QuestionSet"];

/** WHO did it - the audit envelope's one place the word "actor" appears. Every
 *  human action stamps it; a worker's events carry none, which reads as the system. */
export interface ActorRef {
  type: "user" | "agent" | "api_key";
  id: string;
  name: string;
}

interface Base {
  seq?: number; // durable (Lane-1) frames only; Lane-2 deltas carry `after_seq` instead of an SSE id:
  at: string;
  task_id: string;
  step_id?: string | null;
  lane?: string;
  /** The person (or key) behind the event; absent = the system, and every row from before
   *  actors were recorded. The reducer only draws a name when this is present. */
  actor?: ActorRef | null;
}

// Canonical, matching docs/api/events.schema.json $defs (verified against the frozen contract).
export type StepState =
  | "pending"
  | "ready"
  | "running"
  | "verifying"
  | "done"
  | "retrying"
  | "escalated"
  | "skipped";
export type TaskStatus =
  | "created"
  | "clarifying"
  | "planning"
  | "running"
  | "waiting"
  | "paused"
  | "delivered"
  | "failed"
  | "cancelled";
export type Verdict = "pass" | "fail" | "unverified";

// ── durable (Lane 1, carry an SSE id: == seq) ──────────────────────────────────────────────────────
export interface TaskCreated extends Base {
  type: "task.created";
  goal: string;
  parent_task_id?: string | null; // set on a follow-up child - lets the UI walk the conversation chain
}
export interface TaskClarifying extends Base {
  type: "task.clarifying";
}
export interface TaskPlanned extends Base {
  type: "task.planned";
  /** The routed record: which lane took the turn, which clause chose
   *  it (dial | detector | anchor | hatch | planner), and the one-line reason. */
  route?: string;
  route_source?: string;
  route_why?: string;
  /** The planner failed and its one-step floor ran: why. Absent or empty otherwise. */
  fallback?: string;
  // The full plan payload - the schema has ALWAYS sent it (events.gen.ts PlanView); the
  // façade dropped it until the progress tree needed the checklist.
  plan?: {
    steps?: {
      id: string;
      title: string;
      kind?: string;
      state: StepState;
      agent?: string | null;
      attempt?: number;
      why?: string; // One's routing reason (additive)
      bot_version?: number | null; // the plan-time bot pin (additive)
      acceptance?: string[]; // per-step contract criteria (Phase B additive)
      model_ref?: string | null; // fleet seat pin (Phase B additive)
    }[];
    criteria?: { must?: string[]; should?: string[] };
  };
}
export interface TaskStatusChanged extends Base {
  type: "task.status";
  status: TaskStatus;
  reason?: string;
  /** `could_not` on a failure an agent answered (an honest could-not, not a fault). */
  ending?: TaskEnding | null;
  /** On `cancelled`: the code-built account of what the turn finished and never did. */
  summary?: string;
}
/** What a settled turn's answer says it did: `could_not` reads apart from a fault and from done. */
export type TaskEnding = "done" | "partial" | "could_not";
/** The worker picked the turn's workflow off the queue - `task.created →
 * task.dequeued` IS queue hop 1 on the record. A timing mark; the thread renders nothing. */
export interface TaskDequeued extends Base {
  type: "task.dequeued";
  turn: number;
}
/** Queue hop 2, per step - a timing mark; the thread renders nothing. */
export interface StepDequeued extends Base {
  type: "step.dequeued";
}
export interface TaskMoved extends Base {
  type: "task.moved";
  /** Where the task now lives: a Project's id, or null when it went home (personal). */
  to_project_id?: string | null;
}
/** B3: someone set the task's USD ceiling (decimal strings). The gate resolves separately. */
export interface TaskBudgetSet extends Base {
  type: "task.budget_set";
  usd: string;
  previous_usd?: string | null;
}
/** The task's Machine, on the record. */
export interface MachineCreated extends Base {
  type: "machine.created";
  profile: string;
  seeded_as_of?: string | null;
}
export interface MachineSlept extends Base {
  type: "machine.slept";
  reason: string;
  after_s?: number | null;
  dropped_bytes?: number | null;
}
export interface MachineWoke extends Base {
  type: "machine.woke";
  turn: number;
  profile: string;
  browser_restored?: boolean;
  from_park?: boolean;
}
export interface MachineRebuilt extends Base {
  type: "machine.rebuilt";
  reason: string;
  retention_days?: number | null;
}
export interface MachineReset extends Base {
  type: "machine.reset";
}
/** Who drove the Machine's browser, on the record. `actor` is the holder. */
export interface MachineControlClaimed extends Base {
  type: "machine.control_claimed";
  private?: boolean;
}
export interface MachineControlEnded extends Base {
  type: "machine.control_ended";
  /** released | expired | grace | cap | ended | demoted | disabled | lost */
  reason: string;
  held_s?: number;
  /** The Project owner who ended someone else's control (`reason: "ended"`). */
  ended_by?: ActorRef | null;
}

interface TaskRenamed extends Base {
  type: "task.renamed";
  title: string;
}
export interface TaskCancelled extends Base {
  type: "task.cancelled";
}
export interface TaskDelivered extends Base {
  type: "task.delivered";
  summary?: string;
  artifact_ids?: string[];
  /** `partial` when the answer did only part of the work; `could_not` when it declined. */
  ending?: TaskEnding | null;
}
export interface TaskContinued extends Base {
  /** A settled task took a follow-up as its next turn - SAME row, same stream.
   *  Doubles as the status signal: the row is `planning` the moment this lands. */
  type: "task.continued";
  message: string;
  turn: number;
  client_token?: string | null; // the send gesture's token - reconciles the optimistic bubble
  /** A follow-up that changed the cap records both sides ({usd?, tokens?}); the
   *  actor is on the envelope. Typed for the record's readers - the thread does not render it. */
  budget_before?: Record<string, unknown> | null;
  budget_after?: Record<string, unknown> | null;
  epoch?: number | null; // the epoch this turn opened at
}
export interface TaskFeedback extends Base {
  /** The user graded a turn. Append-only; the newest frame is the turn's mood,
   *  and the buttons reset when the next turn opens. */
  type: "task.feedback";
  rating: "up" | "down";
  turn?: number;
}
/** The answerer's soft hatch: a proposal card under a reply. Nothing executed -
 *  Start is a follow-up with the deliverable dial set, Not now is nothing. */
export interface TaskProposed extends Base {
  type: "task.proposed";
  goal: string;
  deliverable?: string | null;
  why?: string;
  turn?: number;
}
export interface PlanEdited extends Base {
  type: "plan.edited";
  /** The edit itself - a steer's kind+reason is what renders as the user's turn. */
  edit?: { kind: string; reason?: string; command_id?: string | null; epoch?: number | null };
}
/** Where a steer or pause took hold. `boundary` is for pause.applied only. */
export type SafePoint =
  | "before_model"
  | "before_tool"
  | "final_answer"
  | "approval"
  | "replan"
  | "rerun"
  | "interrupt"
  | "boundary";
export interface SteerApplied extends Base {
  type: "steer.applied";
  command_id: string;
  step_title?: string;
  author?: string;
  safe_point: SafePoint;
  superseded?: string[];
  /** The step's calls that had already acted outside the task when it took the steer. */
  already_done?: string[];
}
export interface SteerRejected extends Base {
  type: "steer.rejected";
  command_id: string;
  reason: string;
}
export interface SteerRetracted extends Base {
  type: "steer.retracted";
  command_id: string;
}
export interface PauseApplied extends Base {
  type: "pause.applied";
  safe_point: SafePoint;
}
export interface StepStateChanged extends Base {
  type: "step.state";
  state: StepState;
  title: string;
  agent?: string | null;
  attempt: number;
  detail: string;
  model_ref?: string | null; // fleet seat attribution (Phase B additive)
  // A guarded step's typed ending (additive): absent on older frames.
  outcome?: "done" | "partial" | "blocked" | "failed" | null;
  blocked_by?: "login" | "approval" | "person" | "budget" | null;
}
export interface StepNarration extends Base {
  type: "step.narration";
  markdown: string;
}
/** The assembled prompt a step's agent was given - one durable frame per
 *  attempt, clipped server-side with an honest `truncated` marker. The debugging receipt a
 *  bot author reads when "why did my bot do that" needs an answer. */
export interface StepPrompt extends Base {
  type: "step.prompt";
  attempt: number;
  agent: string;
  prompt: string;
  truncated?: boolean;
}
export interface ToolCalled extends Base {
  type: "tool.called";
  tool?: string;
  summary?: string;
  ok?: boolean;
  duration_ms?: number;
  denied_reason?: string;
  tool_call_id?: string; // the live↔durable reconcile key (Phase B additive)
}
export interface GateOpened extends Base {
  type: "gate.opened";
  gate_id: string;
  title?: string;
  kind?: string;
  /** The gate's evidence: an approve gate's per-call blocks ("Connector: tool"
   *  label line + capped args preview, blank-line separated), a budget gate's ceiling
   *  sentence, a connect gate's one-sentence why. Empty for input gates. */
  reason?: string;
  /** CONNECT gates: which service the in-conversation block offers. */
  connector_slug?: string;
  /** APPROVE gates: where the ask came from
   *  ("connector" | "browser"), the host a browser ask is about, and the scopes offered
   *  ("once" | "task" | "always" for an allowlist ask, empty for a danger ask). */
  source?: string;
  host?: string;
  scopes?: string[];
  /** APPROVE gates: why the approval rules asked, each once - the card says it first. */
  asks_because?: string[];
  /** BUDGET gates (B3): which ceiling a "Budget reached" gate hit - "task" or "project", or
   *  "workspace" for a "Spending allowance reached" gate; empty for the long-form cost
   *  confirmation. */
  budget_scope?: string;
  /** BUDGET gates: the Bursar's figures - null for the cost confirmation and older gates. */
  budget_figures?: BudgetFigures | null;
  expires_at?: string;
  questions?: QuestionSet; // the event embeds the whole set: { title, context, questions[], … }
}
/** The money behind a "Budget reached" gate (decimal strings): the ceiling it hit, that ceiling's
 *  spend when it opened, and the coming wave's estimate ("0" when the spend alone reached it).
 *  `exhausted`: the workspace has spent its allowance - a "Spending allowance reached" gate, with
 *  zero dollars. */
export interface BudgetFigures {
  ceiling_usd: string;
  spent_usd: string;
  projected_usd?: string;
  exhausted?: boolean;
}
export interface GateResolved extends Base {
  type: "gate.resolved";
  gate_id?: string;
  /** accept | decline | cancel - and on accept, the validated answers by question id.
   *  What the thread's Q&A record card is built from. */
  action?: "accept" | "decline" | "cancel";
  content?: Record<string, string | number | boolean | string[]> | null;
  /** The ids in `content` the person skipped, filled from defaults; null where not recorded. */
  defaulted?: string[] | null;
}
export interface UsageRecorded extends Base {
  type: "usage.recorded";
  cost_usd: string;
  model: string;
  family: string;
  /** Native units - input_token/output_token/cache_* - the schema always sent them; the
   *  tree's per-step ledger finally reads them. */
  units?: Record<string, number>;
}
export interface ArtifactProduced extends Base {
  type: "artifact.produced";
  artifact_id: string;
  title?: string;
  artifact_type?: string;
  bytes?: number;
  /** deliverable | working. The schema always carried it; the reducer folds on it
   *  so the figure fleet's mid-run PNG batch can't hijack the reader.
   *  Absent (pre-lane rows) = deliverable. */
  role?: string;
}
export interface ArtifactRevised extends Base {
  type: "artifact.revised";
  artifact_id: string; // the NEW row - the lineage's face from now on
  supersedes_id: string;
  lineage_id: string;
  revision: number;
  title?: string;
  artifact_type?: string;
  bytes?: number;
  role?: string; // same contract as ArtifactProduced
}
export interface ArtifactSnapshot extends Base {
  type: "artifact.snapshot";
  artifact_id?: string;
  path?: string;
  offset?: number;
  content?: string;
}
export interface VerificationSigned extends Base {
  type: "verification.signed";
  verdict: Verdict;
  verifier_family?: string | null;
  artifact_id?: string;
  suite?: string;
  signature?: string;
}
export interface QAReviewed extends Base {
  // The long-form lane's pre-delivery editorial verdict - NOT verification. The
  // findings are the rebuild's brief and the follow-up turn's reference.
  type: "qa.reviewed";
  verdict: "pass" | "revise" | "pass_unread";
  attempt?: number;
  findings?: {
    location: string;
    severity: "blocker" | "major" | "minor";
    kind: string;
    instruction: string;
  }[];
  summary?: string;
  reviewer_model?: string;
  reviewer_family?: string | null;
}
export interface EscalationRaised extends Base {
  type: "escalation.raised";
  // The structured Diagnosis (what_failed / why / attempts / evidence / suggested_next). This
  // was hand-typed `string` while the wire sent an object - the drift typechecked and then threw
  // at render. Mirror the generated schema; never narrow it by hand.
  diagnosis?: {
    what_failed: string;
    why: string;
    attempts: number;
    evidence: string[];
    suggested_next?: string[];
  };
}

export interface ContextLaneView {
  lane: "instructions" | "user" | "project" | "bot" | "thread" | "skills";
  tokens: number;
  truncated?: boolean;
  /** The lane's ceiling, from the server; absent on unbudgeted lanes and older events. */
  budget?: number | null;
  items: { preview: string; memory_id?: string | null }[];
}
export interface ContextAssembled extends Base {
  type: "context.assembled";
  lanes: ContextLaneView[];
  total_tokens: number;
  memory_ids?: string[];
}
export interface MemoryWritten extends Base {
  type: "memory.written";
  /** 0 is valid and load-bearing: the archivist ran and kept silence. */
  count: number;
  items?: { id: string; kind: string; text: string }[];
  /** "project": the Project fence stood the archivist down - a Project task mints no private memory. */
  skipped?: string | null;
}
export interface BotMemoryWritten extends Base {
  type: "bot_memory.written";
  /** The DM keeper that learned. 0 is the curator's recorded silence. */
  bot: string;
  count: number;
  items?: { id: string; kind: string; text: string }[];
}

// ── ephemeral (Lane 2, no id:) ─────────────────────────────────────────────────────────────────────
export interface MessageStart extends Base {
  type: "message.start";
  agent: string;
}
export interface MessageDelta extends Base {
  type: "message.delta";
  text: string;
}
export interface MessageEnd extends Base {
  type: "message.end";
}
export interface ArtifactDelta extends Base {
  type: "artifact.delta";
  path: string;
  text: string;
  /** Character offset of `text` in the document; 0 + whole-current is the legacy preview shape. */
  offset?: number;
}
export interface Progress extends Base {
  type: "progress";
  phase: string;
  elapsed_s: number;
  tokens_so_far: number;
  detail: string;
}
export interface Activity extends Base {
  /** One tiny operation, live (the progress tree's rows) - tool call start/end, model
   *  rounds ("Thinking…"), streamed thinking text, engine/phase machinery. Ephemeral:
   *  absent on replay; the durable `tool.called` reconciles by `tool_call_id`. */
  type: "activity";
  kind: "tool" | "model" | "thinking" | "engine" | "phase";
  phase: "start" | "end" | "delta";
  tool?: string;
  label?: string;
  text?: string;
  tool_call_id?: string;
  ok?: boolean | null;
  duration_ms?: number;
  /** A Machine phase's `start`: the bound (seconds) on the step's whole wait for its Machine,
   *  after which the step fails. Absent on older events and every other row. */
  wait_s?: number | null;
}

// ── presence (Lane 2, no id:) ─────────────────────────────────────────────────────────────────────
/** Who is on this task, right now: state about its viewers, not content. Never replayed; the
 *  client keeps a person present while any of their connections said join/here within 30 s. */
interface PresenceBase extends Base {
  conn: string; // one open stream (a browser tab), server-sanitized
  actor: ActorRef;
  editor?: boolean; // false = shown as watching (a viewer)
}
export interface PresenceJoin extends PresenceBase {
  type: "presence.join";
}
export interface PresenceLeave extends PresenceBase {
  type: "presence.leave";
}
export interface PresenceHere extends PresenceBase {
  type: "presence.here";
}
export interface PresenceTyping extends PresenceBase {
  type: "presence.typing";
  on: boolean;
  mode: "follow_up" | "steer" | "answer";
}

// ── the screen (Lane 2, no id:) ─────────────────────────────────────────────────────
/** Who drives the Machine's browser right now - state, never replayed. `holder` null = nobody:
 *  the agent drives. The durable machine.control_* rows draw the timeline. */
export interface ScreenState extends Base {
  type: "screen.state";
  holder?: ActorRef | null;
  private?: boolean;
  until?: string | null;
}
/** An element's box in viewport CSS px (the screencast's own coordinates). */
export interface ScreenBox {
  x: number;
  y: number;
  w: number;
  h: number;
}
export interface ScreenAction extends Base {
  /** An agent's acting verb that succeeded: the pane flashes `box` for 1.5 s. Host
   *  and path only, never a query string. Lane 2: never replayed. */
  type: "screen.action";
  verb: string;
  box?: ScreenBox | null;
  host?: string;
  path?: string;
}

export type TaskEvent =
  | TaskCreated
  | TaskClarifying
  | TaskPlanned
  | TaskStatusChanged
  | TaskMoved
  | TaskBudgetSet
  | MachineCreated
  | MachineSlept
  | MachineWoke
  | MachineRebuilt
  | MachineReset
  | MachineControlClaimed
  | MachineControlEnded
  | TaskRenamed
  | TaskCancelled
  | TaskDelivered
  | TaskContinued
  | TaskProposed
  | TaskFeedback
  | PlanEdited
  | SteerApplied
  | SteerRejected
  | SteerRetracted
  | PauseApplied
  | StepStateChanged
  | StepNarration
  | StepPrompt
  | ToolCalled
  | GateOpened
  | GateResolved
  | UsageRecorded
  | ArtifactProduced
  | ArtifactRevised
  | ArtifactSnapshot
  | VerificationSigned
  | QAReviewed
  | EscalationRaised
  | ContextAssembled
  | MemoryWritten
  | BotMemoryWritten
  | TaskDequeued
  | StepDequeued
  | MessageStart
  | MessageDelta
  | MessageEnd
  | ArtifactDelta
  | Progress
  | Activity
  | PresenceJoin
  | PresenceLeave
  | PresenceHere
  | PresenceTyping
  | ScreenState
  | ScreenAction;

/** SETTLED task statuses - at rest, not sealed: a follow-up continues the same task
 *  as its next turn, so the page keeps its socket open and `task.continued` rides it. */
export const TERMINAL: ReadonlySet<TaskStatus> = new Set(["delivered", "failed", "cancelled"]);

/** Lane-2 (ephemeral) event types: no SSE `id:`, never replayed on resume, always applied live. Every
 *  other type is durable (Lane-1) and deduped by `seq` so a resume never double-applies (e.g. usage). */
export const EPHEMERAL: ReadonlySet<TaskEventType> = new Set([
  "message.start",
  "message.delta",
  "message.end",
  "artifact.delta",
  "progress",
  "activity",
  "presence.join",
  "presence.leave",
  "presence.here",
  "presence.typing",
  "screen.state",
  "screen.action",
]);

/** Every wire type, at runtime - the server names each SSE frame with `event: <type>`, so the stream
 *  hook must register a listener per name (the default `message` handler only sees unnamed frames). */
export const ALL_EVENT_TYPES: readonly TaskEventType[] = [
  "task.created",
  "task.clarifying",
  "task.planned",
  "task.status",
  "task.dequeued",
  "step.dequeued",
  "task.moved",
  "task.budget_set",
  "machine.created",
  "machine.slept",
  "machine.woke",
  "machine.rebuilt",
  "machine.reset",
  "machine.control_claimed",
  "machine.control_ended",
  "task.renamed",
  "task.cancelled",
  "task.delivered",
  "task.continued",
  "task.proposed",
  "task.feedback",
  "plan.edited",
  "steer.applied",
  "steer.rejected",
  "steer.retracted",
  "pause.applied",
  "step.state",
  "step.narration",
  "step.prompt",
  "tool.called",
  "gate.opened",
  "gate.resolved",
  "usage.recorded",
  "artifact.produced",
  "artifact.revised",
  "artifact.snapshot",
  "verification.signed",
  "qa.reviewed",
  "escalation.raised",
  "context.assembled",
  "memory.written",
  "bot_memory.written",
  "message.start",
  "message.delta",
  "message.end",
  "artifact.delta",
  "progress",
  "activity",
  "presence.join",
  "presence.leave",
  "presence.here",
  "presence.typing",
  "screen.state",
  "screen.action",
];
