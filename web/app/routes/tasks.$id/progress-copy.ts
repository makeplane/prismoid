/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The progress tree's voice - every verb, icon, and duration the tree renders, as pure
 *  data (`progress-tree.tsx` renders; this file decides). Extensibility law: a NEW tool
 *  kind is a row in these tables, never a new component - unknown tools fall back to
 *  their raw name with a wrench, so the model's flailing stays visible and safe.
 *
 *  The vocabulary is HONEST: it names only what the engine can actually do. That list
 *  GREW: the web door and the search lane are real tools now,
 *  so "Reading"/"Searching" are earned here where they were fiction before. The rule the
 *  old comment was defending still stands: a verb goes in this table only once the engine
 *  can do the thing. */

import {
  AlertTriangle,
  ArrowLeft,
  Brain,
  Camera,
  Clock,
  Cog,
  Download,
  Eye,
  FileText,
  GitBranch,
  Globe,
  Hand,
  HelpCircle,
  Image,
  Keyboard,
  ListChecks,
  ListTodo,
  MessageCircleQuestion,
  MousePointerClick,
  MoveVertical,
  Pause,
  Search,
  ShieldCheck,
  Sparkles,
  SquareChevronRight,
  TextCursorInput,
  Type,
  Upload,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { BRAND } from "~/components/connectors/brand-icons";
import type { ExecGroup, ExecutionItem, ExecRow, GateView } from "~/lib/task-view";
import { formatUsd } from "~/lib/format";
import { formatDurationMs, formatTokens } from "~/lib/time";
import { callsLabel } from "~/lib/usage-ledger";

type ToolCopy = {
  live: string; // the pending row's verb - "Reading"
  done: string; // the settled row's verb - "Read"
  icon: LucideIcon;
  object?: string; // what the verb acts on when the row has no locator yet - "a page"
  unknown?: true; // the wrench fallback: a name no row knows
};

const TOOL_COPY: Record<string, ToolCopy> = {
  "frame.read_file": { live: "Reading", done: "Read", icon: FileText },
  "frame.shell": { live: "Running", done: "Ran", icon: SquareChevronRight },
  "frame.write_file": { live: "Writing", done: "Wrote", icon: FileText },
  "frame.publish": { live: "Publishing", done: "Published", icon: Upload },
  "frame.egress": { live: "Reaching", done: "Reached", icon: Globe },
  // The worker-process network tools and the media lane. Without a
  // row each of these fell to FALLBACK, whose `done` verb is the RAW journal name - the tree
  // read "Called web.fetch" with a wrench, on the two tools the researcher and the writer now
  // reach for on almost every task. A new tool kind is a row in this table; these are theirs.
  "web.fetch": { live: "Reading", done: "Read", icon: Globe },
  "web.search": { live: "Searching", done: "Searched", icon: Search },
  "media.generate_image": { live: "Drawing", done: "Drew", icon: Image },
  ask_user: { live: "Asking you", done: "Asked", icon: HelpCircle },
  // The browser family (the journal says `browser.open`; the live frame says the model's own
  // `browser_open`, which `toolCopy` folds onto the same row). Without these every page visit
  // read "Calling args: url" under a wrench.
  "browser.open": { live: "Opening", done: "Opened", icon: Globe, object: "a page" },
  "browser.back": { live: "Going back", done: "Went back", icon: ArrowLeft },
  "browser.wait": { live: "Waiting for", done: "Waited for", icon: Clock, object: "the page" },
  "browser.snapshot": { live: "Looking at", done: "Looked at", icon: Eye, object: "the page" },
  "browser.click": { live: "Clicking", done: "Clicked", icon: MousePointerClick },
  "browser.type": { live: "Typing into", done: "Typed into", icon: Keyboard, object: "the page" },
  "browser.fill": { live: "Filling", done: "Filled", icon: TextCursorInput, object: "a form" },
  "browser.select": { live: "Choosing in", done: "Chose in", icon: ListChecks, object: "a list" },
  "browser.press": { live: "Pressing", done: "Pressed", icon: Keyboard, object: "a key" },
  "browser.scroll": { live: "Scrolling", done: "Scrolled", icon: MoveVertical },
  "browser.screenshot": { live: "Taking a screenshot", done: "Took a screenshot", icon: Camera },
  "browser.handoff": {
    live: "Handing you the browser",
    done: "Handed you the browser",
    icon: Hand,
  },
  "browser.allow": { live: "Allowing", done: "Allowed", icon: ShieldCheck },
  "browser.save": { live: "Saving a screenshot", done: "Saved a screenshot", icon: Download },
  // The machinery rows (engine/phase facts): their LABEL is the whole sentence
  // ("Preparing the workspace", "Drafting the plan") - no verb prefix. Phase facts carry
  // WHO on the tool field, so each machinery agent wears its own face.
  engine: { live: "", done: "", icon: Cog },
  machine: { live: "", done: "", icon: Cog }, // the engine's acquire, naming the Machine
  phase: { live: "", done: "", icon: Sparkles },
  clarifier: { live: "", done: "", icon: MessageCircleQuestion },
  planner: { live: "", done: "", icon: GitBranch },
  namer: { live: "", done: "", icon: Type },
  // The wait row: human time on the record ("Waiting on you - 3 questions" →
  // "You answered - 2m 10s"). Its duration is PARKED time, honestly named.
  gate: { live: "", done: "", icon: HelpCircle },
  // A mid-step pause: the step parked before its next tool call until a resume.
  // Its own face - a question mark there read as "waiting on you", and nobody answers a pause.
  pause: { live: "", done: "", icon: Pause },
};

/** WHO is working, as the tree names them - the machinery agents the wire identifies on
 *  phase facts (the "which agent is doing this?" answer). Step agents come
 *  from `group.agent` and wear the group's chip; these name the pre-step actors. */
const ACTOR_LABEL: Record<string, string> = {
  clarifier: "Clarifier",
  planner: "Planner",
  namer: "Namer",
};

export function actorLabel(tool: string): string | null {
  return ACTOR_LABEL[tool] ?? null;
}

const FALLBACK: ToolCopy = { live: "Calling", done: "Called", icon: Wrench };

/** Connector tool verbs: `{slug}_{verb}_{rest}` names are OPEN vocabulary, so a
 *  static table cannot know them - this is the one function branch beside it, and the table
 *  stays first authority (`ask_user` and every registered name never route here). Only a
 *  RECOGNIZED verb earns copy; an unknown verb returns null and the wrench fallback keeps
 *  the model's flailing visible - the same honesty law, extended. */
const CONNECTOR_VERBS: Record<string, { live: string; done: string; icon: LucideIcon }> = {
  search: { live: "Searching", done: "Searched", icon: Search },
  get: { live: "Fetching", done: "Fetched", icon: FileText },
  fetch: { live: "Fetching", done: "Fetched", icon: FileText },
  read: { live: "Reading", done: "Read", icon: FileText },
  list: { live: "Listing", done: "Listed", icon: ListTodo },
  find: { live: "Finding", done: "Found", icon: Search },
  query: { live: "Querying", done: "Queried", icon: Search },
  send: { live: "Sending via", done: "Sent via", icon: Upload },
  create: { live: "Creating in", done: "Created in", icon: FileText },
  add: { live: "Adding to", done: "Added to", icon: FileText },
  update: { live: "Updating in", done: "Updated in", icon: FileText },
  delete: { live: "Deleting in", done: "Deleted in", icon: AlertTriangle },
};

export function connectorToolCopy(raw: string): ToolCopy | null {
  if (raw in TOOL_COPY || raw.includes(".")) return null; // registered names and journal dots stay theirs
  const [slug, verb] = raw.split("_");
  if (!slug || !verb) return null;
  const known = CONNECTOR_VERBS[verb.toLowerCase()];
  if (!known) return null;
  const brand = BRAND[slug]?.name ?? slug.charAt(0).toUpperCase() + slug.slice(1);
  return {
    live: `${known.live} ${brand}`,
    done: `${known.done} ${brand}`,
    icon: known.icon,
  };
}

/** What counts as WORKING - one set, three consumers (the tree's auto-open + highlight +
 *  tail-anchor, the root line's cast, the rail's in-flight meta). `ready` is queued, not
 *  working: it neither opens, nor ticks, nor counts. Three sibling files once kept their
 *  own copies and disagreed on exactly that. */
export const ACTIVE_STEP_STATES: ReadonlySet<string> = new Set([
  "running",
  "verifying",
  "retrying",
]);

/** The panel's text column: icon 14px + gap 8px. Notes and "+N more" indent by this so
 *  every panel reads as one left edge - the value is a law, so it has a name. */
export const TEXT_COL_PAD = "pl-[22px]";

/** The live frame names a browser verb as the model called it (`browser_open`); the journal
 *  says `browser.open`. One row, either spelling. */
function journalName(tool: string): string {
  return tool.startsWith("browser_") ? `browser.${tool.slice("browser_".length)}` : tool;
}

export function toolCopy(tool: string): ToolCopy {
  const name = journalName(tool);
  return TOOL_COPY[name] ?? connectorToolCopy(name) ?? { ...FALLBACK, done: tool, unknown: true };
}

/** A browser summary that only restates its own verb ("back", "scroll down", "press Enter",
 *  "screenshot") keeps just what the verb does not already say. */
const BROWSER_VERB_ECHO =
  /^(?:back|click|type|fill|select|press|scroll|screenshot|save a screenshot)\b\s*/;

/** An exception's class name is the engine's word, not the reader's: "BrowserFailed: browser
 *  timed out after 30.0s" reads as "browser timed out after 30.0s". */
export function plainReason(reason: string): string {
  return reason.replace(/^[A-Z][A-Za-z0-9]*(?:\.[A-Z][A-Za-z0-9]*)*:\s*/, "");
}

/** A call that did not succeed, named for what happened: a refused grant is "Not allowed",
 *  the egress proxy's refusal is "Blocked", everything else (a timeout, a 404, a crash) is
 *  "Failed". Only a person declines, and a declined approval speaks on its gate row. */
function failedLine(
  row: Extract<ExecRow, { t: "tool" }>,
  subject: string,
): { verb: string; detail: string; mono: boolean } {
  const reason = row.deniedReason ?? "";
  if (reason.startsWith("not granted"))
    return { verb: "Not allowed", detail: subject, mono: false };
  const why = plainReason(reason);
  const detail = why ? `${subject} - ${why}` : subject;
  return { verb: row.tool === "frame.egress" ? "Blocked" : "Failed", detail, mono: false };
}

/** A call a steer HELD: chosen before the steer landed, so it never ran - the
 *  journal says `held: Bob steered`. Not a refusal: it reads quiet, never the warning face. */
export function isHeld(row: Extract<ExecRow, { t: "tool" }>): boolean {
  return row.ok === false && (row.deniedReason ?? "").startsWith("held:");
}

/** A denied/failed row is one of the tree's few loud lines (a held one is not). */
export function toolIcon(row: Extract<ExecRow, { t: "tool" }>): LucideIcon {
  if (row.ok === false && !isHeld(row)) return AlertTriangle;
  return toolCopy(row.tool).icon;
}

type ToolLine = { verb: string; detail: string; mono: boolean };

/** "held: Bob and Ada steered" - the names are everything between the prefix and the verb. */
function heldLine(row: Extract<ExecRow, { t: "tool" }>): ToolLine {
  const who = (row.deniedReason ?? "")
    .slice("held:".length)
    .replace(/\s*steered\s*$/, "")
    .trim();
  return {
    verb: "Held",
    detail: `${who || "Someone"} steered before this ran`,
    mono: false,
  };
}

/** A settled search row's summary is the ENGINE that answered (the query is never journaled),
 *  so it reads as the route taken, never as what was searched for. */
function searchLine(row: Extract<ExecRow, { t: "tool" }>): ToolLine {
  const via = row.summary ? `via ${row.summary}` : "";
  if (row.ok === false) return failedLine(row, `web search ${via}`.trim());
  return { verb: "Searched the web", detail: via, mono: false };
}

/** The row's one line: verb + detail (+ whether the detail is a locator that wants mono).
 *  `frame.publish` summaries arrive as "type:path" - read them back as "file · kind";
 *  machinery rows (engine/phase) speak whole sentences in the UI face. */
export function toolLine(row: Extract<ExecRow, { t: "tool" }>): {
  verb: string;
  detail: string;
  mono: boolean;
} {
  const copy = toolCopy(row.tool);
  if (isHeld(row)) return heldLine(row);
  if (row.tool === "web.search" && !row.pending) return searchLine(row);
  if (copy.unknown) {
    // A tool nobody registered: its raw name under the wrench, and no arguments, ever.
    if (row.ok === false) return failedLine(row, row.tool);
    return row.pending
      ? { verb: copy.live, detail: row.tool, mono: true }
      : { verb: row.tool, detail: "", mono: true };
  }
  const detail = locator(row);
  if (row.ok === false) return failedLine(row, detail || row.tool);
  const machinery =
    row.tool === "engine" ||
    row.tool === "machine" ||
    row.tool === "phase" ||
    row.tool === "gate" ||
    row.tool === "pause" ||
    actorLabel(row.tool) != null;
  if (machinery) return { verb: "", detail, mono: false };
  const verb = row.pending ? copy.live : copy.done;
  // No locator yet (a live frame carries only arg keys): the verb's own object reads instead.
  if (!detail) return { verb, detail: copy.object ?? "", mono: false };
  return { verb, detail, mono: true };
}

/** The row's locator, read back for people: publish's "type:path" as "file · kind", egress's
 *  "host=" dropped, a browser summary that only restates its verb trimmed, and a keys-only
 *  live label ("args: url") never shown - argument names are not a locator. */
function locator(row: Extract<ExecRow, { t: "tool" }>): string {
  let detail = row.summary ?? "";
  if (detail.startsWith("args:")) return "";
  if (row.tool === "frame.publish" && detail.includes(":")) {
    const [kind, ...path] = detail.split(":");
    const file = path.join(":").split("/").pop() ?? "";
    detail = file ? `${file} · ${kind}` : detail;
  }
  if (row.tool === "frame.egress" && detail.startsWith("host=")) {
    detail = detail.slice(5);
  }
  if (journalName(row.tool).startsWith("browser.")) {
    detail = detail.replace(BROWSER_VERB_ECHO, "");
  }
  return detail;
}

export const PLAN_ICON = ListTodo;
export const ROUND_ICON = Brain;

/** The model-round row: "Thinking…" while open, "Thought for Ns" once settled. */
export function roundLine(row: Extract<ExecRow, { t: "round" }>): string {
  if (row.pending) return "Thinking…";
  return row.durationMs != null ? `Thought for ${formatDurationMs(row.durationMs)}` : "Thought";
}

/** Group headline: the planner's own step title is already human; only synthetic groups
 *  get minted verbs. */
export function headline(group: ExecGroup): string {
  if (group.kind === "plan") {
    const n = group.checklist?.length ?? 0;
    if (group.state === "done" && n > 0) return `Planned ${n} step${n === 1 ? "" : "s"}`;
    return "Setting up the plan";
  }
  return group.title || "Working";
}

const BLOCKED_LABEL: Record<string, string> = {
  login: "Blocked: sign-in needed",
  approval: "Blocked: approval needed",
  person: "Blocked: needs a person",
  budget: "Blocked: budget reached",
};

/** A guarded step's ending on its row: "Partly done", "Blocked: sign-in
 *  needed". Null for a plain done and for every step without an outcome. */
export function outcomeLabel(group: ExecGroup): string | null {
  if (group.outcome === "partial") return "Partly done";
  if (group.outcome === "blocked")
    return (group.blockedBy && BLOCKED_LABEL[group.blockedBy]) || "Blocked";
  return null;
}

/** A person's pause as the page tells it: `by` is empty when no name should show
 *  (a personal task: the only one pausing is you). */
export type PauseView = { by?: string; applied: boolean };

/** The pause in a phrase: "Pausing… (Ada)" until it takes hold, then "Paused by Ada". */
export function pausePhrase(pause: PauseView): string {
  const by = pause.by?.trim();
  if (!pause.applied) return by ? `Pausing… (${by})` : "Pausing…";
  return by ? `Paused by ${by}` : "Paused";
}

/** The transcript's status line while paused. A pause no one is named for (a legacy row, a
 *  replay from before steering) keeps the line it always had. */
export function pauseStatusLine(pause: PauseView | null): string {
  const rest = "resume when you want the machine to continue";
  if (!pause) return `paused - ${rest}`;
  return pause.applied ? `${pausePhrase(pause)} - ${rest}` : pausePhrase(pause);
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** A stopped turn's tree whose own steps did not all finish says how far it got, never "Completed" (a
 *  tree that finished did complete; the stop's account says what never ran). Null otherwise. */
function stoppedLabel(groups: ExecGroup[], status?: string): string | null {
  if (status !== "cancelled") return null;
  const steps = groups.filter((g) => g.kind === "step");
  // The server marks the step a stop cut off `skipped`: under a cancel it was not done.
  const done = steps.filter((g) => g.state === "done").length;
  return done < steps.length ? `Stopped - ${done} of ${plural(steps.length, "step")} done` : null;
}

/** The settled root line. Skipped steps are named, never silently absorbed into "Completed" -
 *  the root line is the story's one-sentence summary, and a skip is part of the story. A turn
 *  with an issue did not complete: it says how far it got ("1 of 3 steps done · 1 issue"),
 *  never "Completed". The cast counts every named actor across the tree: step agents plus the
 *  machinery that names one (the plan group's planner, only when a planner drafted the plan). */
function settledRootLabel(groups: ExecGroup[], status?: string): string {
  const stopped = stoppedLabel(groups, status);
  if (stopped) return stopped;
  const steps = groups.filter((g) => g.kind === "step");
  const issues = steps.filter((s) => s.state === "escalated").length;
  const skipped = steps.filter((s) => s.state === "skipped").length;
  const finished = steps.filter((s) => s.state === "done").length;
  const total = steps.length || groups.length;
  // A step that never started has no group, so "how far it got" counts the plan's own list
  // (the rail's "0/2" and this line agree).
  const planned = groups.find((g) => g.kind === "plan")?.checklist?.length ?? 0;
  const agents = new Set(groups.map((g) => g.agent).filter((a): a is string => !!a));
  const bits = [
    issues > 0
      ? `${finished} of ${plural(Math.max(total, planned), "step")} done`
      : `Completed ${plural(skipped > 0 ? finished : total, "step")}`,
  ];
  if (skipped > 0) bits.push(`${skipped} skipped`);
  if (agents.size > 1) bits.push(`${agents.size} agents`);
  if (issues > 0) bits.push(plural(issues, "issue"));
  return bits.join(" · ");
}

/** The collapsed root line - the whole execution story in one quiet breath, WITH its
 *  cast: how many agents worked (settled), who is working right now (live), and the
 *  parallel fan-out named when more than one runs at once. */
export function rootLabel(
  item: {
    settled: boolean;
    groups: ExecGroup[];
  },
  status?: string,
  pause?: PauseView | null,
): string {
  if (item.settled) return settledRootLabel(item.groups, status);
  const steps = item.groups.filter((g) => g.kind === "step");
  const done = steps.filter((s) => s.state === "done" || s.state === "skipped").length;
  if (steps.length === 0) return "Getting started";
  // Parked states tell the truth: the machine is NOT working - you are the critical path.
  if (status === "waiting") return `Waiting for your answer - ${done} of ${steps.length} done`;
  if (pause) return `${pausePhrase(pause)} - ${done} of ${steps.length} done`;
  if (status === "paused") return `Paused - ${done} of ${steps.length} done`;
  const active = steps.filter((s) => ACTIVE_STEP_STATES.has(s.state));
  if (active.length > 1) {
    // The fan-out, named: fleet seats / parallel steps working at once.
    return `Working - ${active.length} agents in parallel · ${done} of ${steps.length} done`;
  }
  const position = Math.min(done + 1, steps.length);
  if (active.length === 1) {
    const who = active[0].agent;
    return `Working - step ${position} of ${steps.length}${who ? ` · ${who}` : ""}`;
  }
  return `Working - ${done} of ${steps.length} done`;
}

/** A group's wall-clock, from its own frozen anchors - `~/lib/time`'s scale law. */
export function groupDuration(group: ExecGroup): string | null {
  if (!group.startedAt || !group.endedAt) return null;
  const ms = Date.parse(group.endedAt) - Date.parse(group.startedAt);
  return Number.isFinite(ms) && ms >= 0 ? formatDurationMs(ms) : null;
}

/** The group's own ledger, one quiet clause: "6 model calls · 2 searches · 5.2k tok + 68k cached
 *  · $0.018". Tokens that carry the cost (the cache's) ride beside the fresh ones. */
export function groupLedger(group: ExecGroup): string | null {
  if (!group.calls && !group.searches) return null;
  const bits = [callsLabel(group.calls ?? 0, group.searches ?? 0)];
  if (group.tokens || group.cachedTokens) {
    bits.push(formatTokens(group.tokens ?? 0, group.cachedTokens ?? 0));
  }
  if (group.costUsd) {
    bits.push(formatUsd(group.costUsd));
  }
  return bits.join(" · ");
}

/** The agent chip's identity tint: the categorical ramp indexed by a stable name hash -
 *  the select-option law (index % ramp) carried to agents, so surveyor/gatherer/writer
 *  each keep one recognizable hue across every task. Identity, never status (tone.ts).
 *  ONE hash now: kit tone's `nameHue` - this file and bot-avatar each kept a
 *  byte-identical local copy (one spelled the modulo 8, one HUE_COUNT), the exact drift
 *  a shared ramp exists to prevent. The alias stays: the tree's vocabulary
 *  is agent-shaped, and tests pin the name. */
export { nameHue as agentHueIndex } from "~/components/kit/tone";

export { formatTokens };

/** The TURN's running token sum - every group's folded ledger, one figure for the tail
 *  line (time · tokens · doing, the owner's grammar). */
export function turnTokens(item: ExecutionItem): string | null {
  const n = item.groups.reduce((sum, g) => sum + (g.tokens ?? 0), 0);
  const cached = item.groups.reduce((sum, g) => sum + (g.cachedTokens ?? 0), 0);
  return n > 0 || cached > 0 ? formatTokens(n, cached) : null;
}

/** A group at rest - nothing left for it to do this turn (a skip and an escalation are
 *  rest states too: that step will not run again). The plan group's rest state is its
 *  own `done`. */
const SETTLED_STEP_STATES: ReadonlySet<string> = new Set(["done", "skipped", "escalated"]);

/** What a waiting task waits on, in the tail line's words. A "Spending allowance reached" gate
 *  waits on the allowance coming back, never on anyone's answer; every other gate waits on a
 *  person. */
export function waitingPhrase(gate?: Pick<GateView, "kind" | "budget_scope"> | null): string {
  if (gate?.kind === "budget" && gate.budget_scope === "workspace") {
    return "Spending allowance reached - waiting for it to come back";
  }
  if (gate?.kind === "approve") return "Waiting for your approval";
  if (gate?.kind === "connect") return "Waiting for you to connect";
  return "Waiting for your answer";
}

/** WHAT the machine is doing right now, one honest phrase for the tail line: the newest
 *  in-flight row of the newest active group (verb + object), else that group's own
 *  headline, else the parked/finishing/starting truth. Pure - the tree renders, this
 *  decides. */
export function doingLine(
  item: ExecutionItem,
  status?: string,
  pause?: PauseView | null,
  gate?: Pick<GateView, "kind" | "budget_scope"> | null,
): string {
  // A pause outranks the row in flight: the call finishes, nothing new starts - "Pausing…"
  // is the truth until it takes hold. Unnamed here, always: the transcript's status
  // line is the one home of "Paused by Ada", so the name never shows twice.
  if (pause) return pausePhrase({ applied: pause.applied });
  // A task parked on a gate is waiting on someone: its step still reads `running` (no step
  // frame says otherwise), so the step's own headline would name work that is not happening.
  if (status === "waiting") return waitingPhrase(gate);
  for (let i = item.groups.length - 1; i >= 0; i--) {
    const g = item.groups[i];
    const active = g.kind === "plan" ? g.state !== "done" : ACTIVE_STEP_STATES.has(g.state);
    if (!active) continue;
    for (let j = g.rows.length - 1; j >= 0; j--) {
      const row = g.rows[j];
      if (row.t === "tool" && row.pending) {
        const { verb, detail } = toolLine(row);
        const line = [verb, detail].filter(Boolean).join(" ");
        if (line) return line;
      }
      if (row.t === "round" && row.pending) return "Thinking…";
    }
    return headline(g);
  }
  if (status === "paused") return "Paused";
  // The post-steps window: every group has settled but the
  // turn has not - harvest, verification hand-off, the summary are what runs now. Every
  // task crosses this stretch, and the phrase is aria-live: "Getting started" here read
  // as the task rewinding to zero. That phrase stays for the genuinely-before-anything
  // case (no groups yet, or steps minted but none begun).
  if (
    item.groups.length > 0 &&
    item.groups.every((g) =>
      g.kind === "plan" ? g.state === "done" : SETTLED_STEP_STATES.has(g.state),
    )
  ) {
    return "Finishing up";
  }
  return "Getting started";
}

/** The TURN's settled wall-clock: first group start to last group end, frozen anchors
 *  only (the duration scale law rides formatDurationMs). Null until both ends exist -
 *  a legacy replay without anchors shows no receipt rather than a guess. */
export function turnDuration(item: ExecutionItem): string | null {
  let first = Number.POSITIVE_INFINITY;
  let last = Number.NEGATIVE_INFINITY;
  for (const g of item.groups) {
    if (g.startedAt) first = Math.min(first, Date.parse(g.startedAt));
    if (g.endedAt) last = Math.max(last, Date.parse(g.endedAt));
  }
  if (!Number.isFinite(first) || !Number.isFinite(last) || last < first) return null;
  return formatDurationMs(last - first);
}
