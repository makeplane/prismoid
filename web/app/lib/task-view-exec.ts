/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The execution tree's fold helpers: find-or-mint a turn's phase tree, upsert its groups and
// rows copy-on-write, settle it, and total it for the epilogue's receipt. A leaf beside the
// feed helpers, shared by every event family that touches the tree.

import type { StepState } from "~/lib/api/events";
import type {
  EpilogueItem,
  ExecGroup,
  ExecRow,
  ExecutionItem,
  FeedItem,
  ReceiptRow,
  TaskView,
} from "./task-view-types";

/** Find-or-mint the live turn's execution item, apply `mutate`, and return the updated
 *  view slices. The item is located by `execId` (tail scan by id - inserts/filters can't
 *  stale it); when none is live, a fresh one is appended. `mutate` must be pure
 *  (copy-on-write) - the item it returns replaces the old one in a copied feed. */
/** Content that ends a machinery phase: once one of these lands after the current tree,
 *  resuming machinery starts a NEW phase item (see withExec's phase law). */
function isPhaseContent(f: FeedItem): boolean {
  return f.kind === "segment" || f.kind === "artifact" || f.kind === "working-artifacts";
}

/** Whether this turn's trees already hold `groupId`. */
export function ownsGroup(v: TaskView, groupId: string): boolean {
  return ownerIndex(v, groupId) !== -1;
}

/** The index of this turn's tree that already owns `groupId`, or -1. */
function ownerIndex(v: TaskView, groupId: string): number {
  for (let i = v.feed.length - 1; i >= 0; i--) {
    const item = v.feed[i];
    if (
      item.kind === "execution" &&
      item.turn === v.turn &&
      item.groups.some((g) => g.id === groupId)
    )
      return i;
  }
  return -1;
}

/** The index of the frontier tree (`execId`), or -1 when none is live or, at a step boundary,
 *  content has landed below it since (the caller then opens a new phase tree). */
function frontierIndex(v: TaskView, phase: boolean): number {
  if (v.execId == null) return -1;
  for (let i = v.feed.length - 1; i >= 0; i--) {
    const item = v.feed[i];
    if (item.kind !== "execution" || item.id !== v.execId) continue;
    return phase && v.feed.slice(i + 1).some(isPhaseContent) ? -1 : i;
  }
  return -1;
}

/** withExec with OWNERSHIP: `groupId` names the step group this frame belongs to, so it
 *  folds into whichever phase tree already owns that group (a parallel wave stays whole
 *  while siblings' output lands below it).
 *
 *  THE PHASE LAW (round six, owner: "why is it not coming in midway?"). The record
 *  interleaves work and output; one monolithic tree pinned after the prompt destroyed
 *  that chronology. Phases open at STEP BOUNDARIES ONLY (`phase: true`, passed solely by
 *  step.state): a step's first durable frame arriving after content (a narration, a
 *  card, a chip row) starts a NEW tree at the tail. Nothing else may mint - the live
 *  lane pre-seeds groups from ephemeral activity frames the replay never sees, and a
 *  content-triggered mint on those frames split the two lanes differently (the
 *  tree-parity law caught it). `step.state running` is durable and identically ordered
 *  in both lanes, so both fold to one transcript: work → output → work → output. */
export function withExec(
  v: TaskView,
  groupId: string | null | undefined,
  mutate: (item: ExecutionItem) => ExecutionItem,
  opts?: { phase?: boolean },
): Pick<TaskView, "feed" | "execId" | "nextId"> {
  const owner = groupId != null ? ownerIndex(v, groupId) : -1;
  const at = owner !== -1 ? owner : frontierIndex(v, !!opts?.phase);
  if (at !== -1) {
    const feed = v.feed.slice();
    feed[at] = mutate(v.feed[at] as ExecutionItem);
    return { feed, execId: v.execId, nextId: v.nextId };
  }
  const fresh: ExecutionItem = {
    kind: "execution",
    id: `x${v.nextId}`,
    turn: v.turn,
    settled: false,
    groups: [],
  };
  return {
    feed: [...v.feed, mutate(fresh)],
    execId: fresh.id,
    nextId: v.nextId + 1,
  };
}

/** Copy-on-write group upsert: returns the item with the group replaced (or appended),
 *  mutating nothing. Only the touched group gets a new object - memo'd siblings skip. */
export function upsertGroup(
  item: ExecutionItem,
  id: string,
  update: (g: ExecGroup) => ExecGroup,
  seed: () => ExecGroup,
): ExecutionItem {
  const i = item.groups.findIndex((g) => g.id === id);
  const groups = item.groups.slice();
  if (i === -1) groups.push(update(seed()));
  else groups[i] = update(groups[i]);
  return { ...item, groups };
}

export const stepGroupSeed =
  (id: string, ev: { title?: string; agent?: string | null; attempt?: number }) =>
  (): ExecGroup => ({
    id,
    kind: "step",
    title: ev.title ?? "",
    state: "pending",
    agent: ev.agent,
    attempt: ev.attempt ?? 1,
    rows: [],
  });

/** The synthetic planning group's seed - six call sites used to retype this literal and
 *  drift on `state`. "done" when the mint follows the plan (task.planned and after);
 *  "running" when pre-plan machinery mints it (gates, context, phase facts). It names no
 *  agent: who drafted the plan is `task.planned`'s to say (`route_source`), and a
 *  plan the Lead or a recipe made has no planner to count.
 *  `startedAt` is the minting frame's own `at` - the pre-step CLOCK anchor: before any
 *  step runs, the tail's live clock reads the first group that has a start, and a plan
 *  group without one left the whole clarifying/planning stretch to the heartbeat's
 *  once-per-frame jumps (a clock should tick). Best-effort by design: each
 *  lane anchors on the earliest frame IT carries, and a frame without `at` stamps
 *  nothing rather than guessing. */
export const planGroupSeed =
  (
    state: StepState,
    checklist?: { id: string; title: string; state: StepState }[],
    startedAt?: string,
  ) =>
  (): ExecGroup => ({
    id: "plan",
    kind: "plan",
    title: "",
    state,
    attempt: 1,
    ...(startedAt ? { startedAt } : {}),
    ...(checklist ? { checklist } : {}),
    rows: [],
  });

/** The right seed for whatever group id a stray frame resolves to: the plan group is a
 *  PLAN group even when the frame that mints it arrives before `task.planned` - a step
 *  seed here would freeze `kind: "step"` onto the planning
 *  machinery, and `task.planned`'s update never repairs `kind`. `at` reaches only the
 *  plan seed (its clock anchor above); step groups anchor on step.state `running`. */
export const groupSeed = (gid: string, at?: string) =>
  gid === "plan" ? planGroupSeed("running", undefined, at) : stepGroupSeed(gid, {});

/** Upsert a tool row by id - the live `activity` start and the durable `tool.called`
 *  carry the same `tool_call_id`, so both lanes land on one row (no flicker, exact
 *  replay parity). Missing start (durable-only replay) appends. */
export function mergeToolRow(
  g: ExecGroup,
  id: string,
  patch: Partial<Extract<ExecRow, { t: "tool" }>> & { tool: string },
): ExecGroup {
  const i = g.rows.findIndex((r) => r.t === "tool" && r.id === id);
  const rows = g.rows.slice();
  if (i === -1) rows.push({ t: "tool", id, pending: false, ...patch });
  else rows[i] = { ...(rows[i] as Extract<ExecRow, { t: "tool" }>), ...patch };
  return { ...g, rows };
}

/** Has the durable record denied this row's call (a refusal, or a call a steer held)? Only
 *  `tool.called` carries a denial - the live lane never does. */
export function deniedOnRecord(g: ExecGroup, id: string): boolean {
  return g.rows.some((r) => r.t === "tool" && r.id === id && r.ok === false && !!r.deniedReason);
}

/** The group a stray (step_id-less) frame belongs to: the last step group, else plan. */
export function tailGroupId(item: ExecutionItem): string {
  for (let i = item.groups.length - 1; i >= 0; i--) {
    if (item.groups[i].kind === "step") return item.groups[i].id;
  }
  return "plan";
}

/** Settle the live execution item (delivery, failure, cancellation, next turn). */
export function settleExec(v: TaskView): Pick<TaskView, "feed" | "execId"> {
  // Every unsettled phase tree folds to its quiet header - a turn may have minted
  // several (the phase law above), and delivery closes them all.
  let changed = false;
  const feed = v.feed.map((f) => {
    if (f.kind === "execution" && !f.settled) {
      changed = true;
      return { ...f, settled: true };
    }
    return f;
  });
  return { feed: changed ? feed : v.feed, execId: null };
}

/** One group as the receipt's x-ray row: its own span and tokens, absent when unknown. */
function receiptRow(g: ExecGroup): ReceiptRow {
  const span =
    g.startedAt && g.endedAt ? Date.parse(g.endedAt) - Date.parse(g.startedAt) : undefined;
  return {
    title: g.title || (g.kind === "plan" ? "Planning" : ""),
    agent: g.agent,
    kind: g.kind,
    state: g.state,
    ...(span != null && span >= 0 ? { durationMs: span } : {}),
    ...(g.tokens ? { tokens: g.tokens } : {}),
    ...(g.cachedTokens ? { cachedTokens: g.cachedTokens } : {}),
  };
}

/** Wall clock between two anchors; undefined when either never arrived or they run backwards. */
function spanMs(first: number, last: number): number | undefined {
  return Number.isFinite(first) && Number.isFinite(last) && last >= first
    ? last - first
    : undefined;
}

/** The turn's totals across ALL its phase trees - steps and distinct agents counted,
 *  tokens summed from the groups' folded ledgers, duration as WALL CLOCK (first anchor
 *  to last anchor, gate waits included: "total time" is what the user lived through).
 *  Undefined when the turn ran no machinery at all. */
export function turnReceipt(feed: FeedItem[], turn: number): EpilogueItem["receipt"] {
  let steps = 0;
  let tokens = 0;
  let cachedTokens = 0;
  let first = Number.POSITIVE_INFINITY;
  let last = Number.NEGATIVE_INFINITY;
  const agents = new Set<string>();
  const rows: ReceiptRow[] = [];
  for (const item of feed) {
    if (item.kind !== "execution" || item.turn !== turn) continue;
    for (const g of item.groups) {
      if (g.kind === "step") steps += 1;
      if (g.agent) agents.add(g.agent);
      tokens += g.tokens ?? 0;
      cachedTokens += g.cachedTokens ?? 0;
      if (g.startedAt) first = Math.min(first, Date.parse(g.startedAt));
      if (g.endedAt) last = Math.max(last, Date.parse(g.endedAt));
      rows.push(receiptRow(g));
    }
  }
  if (rows.length === 0) return undefined;
  const durationMs = spanMs(first, last);
  return {
    steps,
    agents: agents.size,
    ...(durationMs != null ? { durationMs } : {}),
    ...(tokens > 0 ? { tokens } : {}),
    ...(cachedTokens > 0 ? { cachedTokens } : {}),
    rows,
  };
}
