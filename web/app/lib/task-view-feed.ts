/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The feed's shared fold helpers: control lines, markers, the segment folds that keep one answer
// told in two lanes as one bubble, and the small upserts every event family reuses. A leaf: it
// imports only the view's types, so any family reducer may use it without a cycle.

import type { ActorRef } from "~/lib/api/events";
import { personName } from "~/lib/format";
import type {
  ArtifactView,
  EpilogueItem,
  FeedItem,
  FeedTone,
  MarkerGlyph,
  Segment,
  StepView,
  TaskView,
} from "./task-view-types";

/** The author a user row wears, off the event's actor. Undefined = nobody is
 *  named, and the row draws no name. */
export function authorOf(
  actor: ActorRef | null | undefined,
): { id: string; name: string } | undefined {
  return actor ? { id: actor.id, name: personName(actor.name) } : undefined;
}

/** Append a control line - or nothing, when the event named no one. Control
 *  lines are not phase content: machinery after one still joins its tree. */
export function withControl(v: TaskView, text: string | null): TaskView {
  return text
    ? {
        ...v,
        feed: [...v.feed, { kind: "control", id: `c${v.nextId}`, text }],
        nextId: v.nextId + 1,
      }
    : v;
}

export function marker(v: TaskView, tone: FeedTone, glyph: MarkerGlyph, text: string): FeedItem[] {
  return [...v.feed, { kind: "marker", id: `m${v.nextId}`, tone, glyph, text }];
}

/** Whitespace-collapsed text - the stream and its durable narration are the same answer in two
 *  lanes, and markdown renders them identically, so the fold compares them the same way. */
export function squash(s: string): string {
  return s.replace(/\s+/g, " ").trim();
}

/** A ladder finding made legible: markdown bold off, the leading list dash and the machine
 *  label ("nothing was published.") dropped - the surrounding copy already says that. */
export function plainFinding(text?: string): string {
  return (text ?? "")
    .replace(/\*\*/g, "")
    .replace(/^-\s*/, "")
    .replace(/^nothing was published\.\s*/i, "")
    .trim();
}

/** …and clipped to one marker-sized sentence. */
export function stripFinding(text?: string): string {
  const sentence = plainFinding(text).split(/(?<=\.)\s/)[0] ?? "";
  return sentence.length > 160 ? `${sentence.slice(0, 157)}…` : sentence;
}

/** A feed item that MARKS a turn: the user's bubble (ask, steer, follow-up) or a prior
 *  turn's closing epilogue. The answer folds below reconcile ONE answer told in two lanes
 *  moments apart - they may scan back over step chrome (cards, markers, the tree), never
 *  over one of these: a prior turn's text must neither absorb nor vouch for this turn's.
 *  A follow-up's short reply ("Done.") is contained in almost any long earlier answer,
 *  and a scan that crossed the boundary spliced the whole reply away. */
export function turnMark(item: FeedItem): boolean {
  return item.kind === "user" || item.kind === "epilogue";
}

/** The newest segment OF THE CURRENT TURN - the scan stops at a turn mark, so a fold can
 *  never reach back into an earlier turn's prose. */
export function lastSegmentIndex(feed: FeedItem[]): number {
  for (let i = feed.length - 1; i >= 0; i--) {
    if (feed[i].kind === "segment") return i;
    if (turnMark(feed[i])) return -1;
  }
  return -1;
}

/** Fold `markdown` against the feed's last segment: if either text contains the other (whitespace
 *  aside) they are one answer - keep one bubble and let the longer (durable) text win. Returns the
 *  updated feed, or null when the text is genuinely new and the caller should append. */
export function foldIntoLastSegment(feed: FeedItem[], markdown: string): FeedItem[] | null {
  const i = lastSegmentIndex(feed);
  if (i === -1) return null;
  const seg = feed[i] as Segment;
  const a = squash(seg.markdown);
  const b = squash(markdown);
  if (a !== b && !a.includes(b) && !b.includes(a)) return null;
  const copy = feed.slice();
  copy[i] = {
    ...seg,
    live: false,
    markdown: b.length > a.length ? markdown : seg.markdown,
  };
  return copy;
}

/** True when every block of the summary already stands in some segment OF THIS TURN
 *  (whitespace aside): nothing new to show, the append would be an echo. The single-
 *  segment fast path stays foldIntoLastSegment (it also upgrades the kept text to the
 *  longer copy). Only the current turn's segments can vouch - the scan collects back to
 *  the nearest turn mark (turn 1's opening bubble included; a feed with no user item is
 *  all one turn). An earlier turn's identical text is a DIFFERENT answer: matching
 *  against it swallowed a follow-up's whole delivery (the cross-turn sibling
 *  of the message.end splice). */
export function summaryAlreadyShown(feed: FeedItem[], summary: string): boolean {
  const blocks = summary
    .split("\n\n")
    .map(squash)
    .filter((b) => b !== "");
  if (blocks.length === 0) return true;
  const segments: string[] = [];
  for (let i = feed.length - 1; i >= 0; i--) {
    const f = feed[i];
    if (turnMark(f)) break;
    if (f.kind === "segment") segments.push(squash(f.markdown));
  }
  return blocks.every((b) => segments.some((s) => s.includes(b)));
}

/** Where a step's answer belongs when it must be APPENDED (a replay has no live segment to
 *  fold into): before the trailing run of the step's execution chrome - artifact cards and
 *  verified/failed/retry/escalation/gate markers. Live, the segment opened at message.start,
 *  BEFORE any of that chrome existed; the durable record writes the narration AFTER it. Same
 *  events, two orders - and a refresh must not move the artifact card (observed:
 *  live read [answer][card], replay read [card][answer]). One fold, one order. */
// Only verified/failed markers still mint (see MarkerGlyph) - the rest are kept so the
// ordering law holds over any older feed shape that reaches this fold.
const STEP_CHROME_GLYPHS = new Set(["verified", "failed", "retry", "escalation", "gate", "tool"]);

export function insertBeforeStepChrome(feed: FeedItem[], item: FeedItem): FeedItem[] {
  let at = feed.length;
  while (at > 0) {
    const prev = feed[at - 1];
    const chrome =
      prev.kind === "artifact" ||
      prev.kind === "escalation" ||
      (prev.kind === "marker" && STEP_CHROME_GLYPHS.has(prev.glyph));
    if (!chrome) break;
    at -= 1;
  }
  return [...feed.slice(0, at), item, ...feed.slice(at)];
}

/** Fold into the live turn's epilogue (post-answer facts: verdicts, memories). Returns
 *  null when none is live - the caller keeps its legacy presentation (fleet seats verify
 *  MID-run, before any epilogue exists). */
export function withEpilogue(
  v: TaskView,
  mutate: (item: EpilogueItem) => EpilogueItem,
): Pick<TaskView, "feed"> | null {
  if (v.epilogueId == null) return null;
  for (let i = v.feed.length - 1; i >= 0; i--) {
    const item = v.feed[i];
    if (item.kind === "epilogue" && item.id === v.epilogueId) {
      const feed = v.feed.slice();
      feed[i] = mutate(item);
      return { feed };
    }
  }
  return null;
}

export function upsertStep(steps: StepView[], next: StepView): StepView[] {
  const i = steps.findIndex((s) => s.id === next.id);
  if (i === -1) return [...steps, next];
  const copy = steps.slice();
  copy[i] = { ...copy[i], ...next };
  return copy;
}

export function upsertArtifact(list: ArtifactView[], next: ArtifactView): ArtifactView[] {
  const i = list.findIndex((a) => a.id === next.id);
  if (i === -1) return [...list, next];
  const copy = list.slice();
  copy[i] = { ...copy[i], ...next };
  return copy;
}

/** Settled and idle: the turn is terminal AND no execution item is live. A straggler
 *  ephemeral frame that arrives now must be DROPPED - a late message.start/delta mints a
 *  ghost typing bubble, a late progress or artifact.delta resurrects `live`/`draft` over a
 *  finished turn (the `activity` case has guarded this since the tree landed).
 *  message.end is EXEMPT: it CLOSES liveness, so a straggler end still settles a segment a
 *  no-summary delivery/cancel left open, rather than stranding its cursor. */
export function settledStraggler(v: TaskView): boolean {
  return v.execId == null && (v.delivered || v.status === "failed" || v.status === "cancelled");
}
