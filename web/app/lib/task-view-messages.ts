/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The answer's folds: the streamed message (start, delta, end), its durable narration, the live
// activity facts and the durable tool calls that fill the execution tree, and the progress
// heartbeat. Live and replayed frames land on the same bubbles and rows by construction.

import type { TaskEvent } from "~/lib/api/events";
import type { ExecRow, Ev, FeedItem, FoldTable, Segment, TaskView } from "./task-view-types";
import {
  deniedOnRecord,
  groupSeed,
  mergeToolRow,
  tailGroupId,
  upsertGroup,
  withExec,
} from "./task-view-exec";
import {
  foldIntoLastSegment,
  insertBeforeStepChrome,
  lastSegmentIndex,
  settledStraggler,
  squash,
  turnMark,
} from "./task-view-feed";

type ActivityEvent = Extract<TaskEvent, { type: "activity" }>;

/** `activity kind="tool"`: a pending row at start (row id = `tool_call_id`, the reconcile
 *  key the durable `tool.called` merges into); the end settles it in place. */
function foldToolActivity(
  v: TaskView,
  ev: ActivityEvent,
): Pick<TaskView, "feed" | "execId" | "nextId"> {
  const rowId = ev.tool_call_id || `t${v.nextId}`;
  const slices = withExec(v, ev.step_id, (item) => {
    const gid = ev.step_id ?? tailGroupId(item);
    return upsertGroup(
      item,
      gid,
      (g) =>
        // The record's denial wins (a held call): a call a steer held ran nothing, yet
        // its live end reads ok - once the durable `tool.called` said why it did not run, a
        // live frame never re-settles the row, whichever lands first.
        deniedOnRecord(g, rowId)
          ? g
          : mergeToolRow(g, rowId, {
              tool: ev.tool ?? "tool",
              ...(ev.phase === "start"
                ? { summary: ev.label, pending: true }
                : {
                    pending: false,
                    ok: ev.ok ?? undefined,
                    durationMs: ev.duration_ms,
                  }),
            }),
      groupSeed(gid, ev.at),
    );
  });
  return {
    ...slices,
    nextId: ev.tool_call_id ? slices.nextId : slices.nextId + 1,
  };
}

/** `activity kind="model"/"thinking"`: ONE round row per group ("Thinking…" → "Thought
 *  for Ns"), REPLACED in place round over round - the counter lives in the label, and a
 *  settled group keeps only its last round's duration. Thinking deltas stream into the
 *  open row; a new round must not EAT reasoning already shown - text that was shown is
 *  text that stays. */
function foldRoundActivity(
  v: TaskView,
  ev: ActivityEvent,
): Pick<TaskView, "feed" | "execId" | "nextId"> {
  const roundId = `r-${ev.step_id ?? "task"}`;
  return withExec(v, ev.step_id, (item) => {
    const gid = ev.step_id ?? tailGroupId(item);
    return upsertGroup(
      item,
      gid,
      (g) => {
        const i = g.rows.findIndex((r) => r.t === "round" && r.id === roundId);
        const rows = g.rows.slice();
        const prev = i !== -1 ? (rows[i] as Extract<ExecRow, { t: "round" }>) : null;
        const next: Extract<ExecRow, { t: "round" }> =
          ev.kind === "thinking"
            ? {
                t: "round",
                id: roundId,
                pending: true,
                ...(prev ?? {}),
                thinkingText: (prev?.thinkingText ?? "") + (ev.text ?? ""),
              }
            : ev.phase === "start"
              ? {
                  t: "round",
                  id: roundId,
                  pending: true,
                  thinkingText: prev?.thinkingText,
                }
              : {
                  t: "round",
                  id: roundId,
                  pending: false,
                  durationMs: ev.duration_ms,
                  thinkingText: prev?.thinkingText,
                };
        if (i === -1) rows.push(next);
        else rows[i] = next;
        return { ...g, rows };
      },
      groupSeed(gid, ev.at),
    );
  });
}

/** `activity kind="engine"/"phase"`: machinery facts. Stages are SERIAL per actor within
 *  a group, so an `end` settles that actor's pending row in place (labels may
 *  legitimately differ: "Collecting the deliverable" → "Collected 1 deliverable") - else
 *  it appends a settled one-liner; a `start` opens a pending row. Phase facts carry WHO
 *  on `ev.tool` (clarifier/planner/namer) - identity is the row's tool, so the
 *  clarifier's end can never settle the namer's concurrent start. */
function foldMachineryActivity(
  v: TaskView,
  ev: ActivityEvent,
): Pick<TaskView, "feed" | "execId" | "nextId"> {
  const machineTool = ev.tool || (ev.kind === "phase" ? "phase" : "engine");
  const rowSeed = `act${v.nextId}`;
  const slices = withExec(v, ev.step_id ?? "plan", (item) => {
    const gid = ev.step_id ?? "plan";
    return upsertGroup(
      item,
      gid,
      (g) => {
        const rows = g.rows.slice();
        const pendingIdx = rows.findIndex(
          (r) => r.t === "tool" && r.tool === machineTool && r.pending,
        );
        if (ev.phase === "end" && pendingIdx !== -1) {
          rows[pendingIdx] = {
            ...(rows[pendingIdx] as Extract<ExecRow, { t: "tool" }>),
            summary: ev.label,
            pending: false,
            ok: true,
            durationMs: ev.duration_ms,
          };
        } else {
          rows.push({
            t: "tool",
            id: rowSeed,
            tool: machineTool,
            summary: ev.label,
            pending: ev.phase === "start",
            ...(ev.phase === "start" && ev.wait_s ? { waitS: ev.wait_s } : {}),
            ...(ev.phase === "end" ? { ok: true, durationMs: ev.duration_ms } : {}),
          });
        }
        return { ...g, rows };
      },
      groupSeed(gid, ev.at),
    );
  });
  return { ...slices, nextId: slices.nextId + 1 };
}

/** Fold a step's narration into the feed. The stream usually carried this text already
 *  (sometimes minus a lead-in, and the live segment may have been closed by message.end before
 *  this frame lands) - fold into that bubble rather than render the answer twice. Only
 *  genuinely new text is a new turn. */
function foldNarration(v: TaskView, markdown: string): Pick<TaskView, "feed" | "nextId"> {
  const folded = foldIntoLastSegment(v.feed, markdown);
  if (folded) return { feed: folded, nextId: v.nextId };
  let feed = v.feed.slice();
  const i = lastSegmentIndex(feed);
  if (i !== -1 && (feed[i] as Segment).live) {
    feed[i] = { ...(feed[i] as Segment), live: false };
  }
  // Appended on replay: land where message.start sat live - before the step's chrome.
  feed = insertBeforeStepChrome(feed, {
    kind: "segment",
    id: `s${v.nextId}`,
    agent: "assistant",
    markdown,
    live: false,
  });
  return { feed, nextId: v.nextId + 1 };
}

/** The open (live) segment, newest first, or -1. */
function liveSegmentIndex(feed: FeedItem[]): number {
  for (let i = feed.length - 1; i >= 0; i--) {
    const f = feed[i];
    if (f.kind === "segment" && f.live) return i;
  }
  return -1;
}

/** The nearest segment before `from` that may be its durable twin. The scan walks back over
 *  step chrome to find the durable twin, but it STOPS at a turn mark: a follow-up's short reply
 *  is contained in almost any long prior answer, and crossing the boundary spliced turn 2's
 *  whole reply away. Same turn folds keep working; other turns' text is another
 *  answer, however contained. */
function twinIndex(feed: FeedItem[], from: number): number {
  for (let j = from - 1; j >= 0; j--) {
    if (turnMark(feed[j])) return -1;
    if (feed[j].kind === "segment") return j;
  }
  return -1;
}

/** message.end: settle the open segment. The two-lanes race (seen in the
 *  email cards' height): the coalesced Lane-2 batch (start/deltas/end) can land AFTER Lane-1's
 *  immediate step.narration already folded the answer into a settled bubble - the echo then
 *  re-accumulates the SAME text in a fresh segment and the thread shows it twice. The end frame
 *  is the one place the echo's FULL text exists, so dedup here: if the just-settled segment and
 *  the previous segment contain one another (whitespace aside), they are one answer - keep one
 *  bubble, longest text wins. An empty ghost (start+end with every delta echo-dropped upstream
 *  or none sent) is contained in anything and vanishes the same way. */
function endLiveSegment(from: FeedItem[]): FeedItem[] {
  const feed = from.slice();
  const i = liveSegmentIndex(feed);
  if (i === -1) return feed;
  const seg: Segment = { ...(feed[i] as Segment), live: false };
  feed[i] = seg;
  const j = twinIndex(feed, i);
  if (j === -1) return feed;
  const prev = feed[j] as Segment;
  const a = squash(prev.markdown);
  const b = squash(seg.markdown);
  if (a !== b && !a.includes(b) && !b.includes(a)) return feed;
  if (b.length > a.length) feed[j] = { ...prev, markdown: seg.markdown };
  feed.splice(i, 1);
  return feed;
}

function messageStart(v: TaskView, ev: Ev<"message.start">): TaskView {
  // A late start after settlement would mint an empty ghost typing bubble - drop it.
  if (settledStraggler(v)) return v;
  // Text is flowing - progress heartbeats are dead-air only. Clear the stretch
  // so the ticker does not keep a stale "this step" clock while tokens pour.
  return {
    ...v,
    live: null,
    feed: [
      ...v.feed,
      {
        kind: "segment",
        id: `s${v.nextId}`,
        agent: ev.agent,
        markdown: "",
        live: true,
      },
    ],
    nextId: v.nextId + 1,
  };
}

function messageDelta(v: TaskView, ev: Ev<"message.delta">): TaskView {
  // A late delta after settlement would resurrect a ghost typing bubble - drop it.
  if (settledStraggler(v)) return v;
  const feed = v.feed.slice();
  // Append to the open (last live) segment; if none is open, start one.
  for (let i = feed.length - 1; i >= 0; i--) {
    const item = feed[i];
    if (item.kind === "segment" && item.live) {
      feed[i] = { ...item, markdown: item.markdown + ev.text };
      return { ...v, feed, live: null };
    }
    if (item.kind === "segment") break;
  }
  return {
    ...v,
    live: null,
    feed: [
      ...v.feed,
      {
        kind: "segment",
        id: `s${v.nextId}`,
        agent: "assistant",
        markdown: ev.text,
        live: true,
      },
    ],
    nextId: v.nextId + 1,
  };
}

function messageEnd(v: TaskView): TaskView {
  return { ...v, feed: endLiveSegment(v.feed), live: null };
}

/** The durable terminal of the message stream: the step's answer, full markdown. */
function stepNarration(v: TaskView, ev: Ev<"step.narration">): TaskView {
  return { ...v, ...foldNarration(v, ev.markdown) };
}

/** The durable record of one tool call becomes (or completes) a row in its step's
 *  group. Row id = tool_call_id when the wire carries one - the SAME id the live
 *  `activity` start used, so replay and live produce one identical row. */
function toolCalled(v: TaskView, ev: Ev<"tool.called">): TaskView {
  const rowId = ev.tool_call_id || `t${v.nextId}`;
  const slices = withExec(v, ev.step_id, (item) => {
    const gid = ev.step_id ?? tailGroupId(item);
    return upsertGroup(
      item,
      gid,
      (g) =>
        mergeToolRow(g, rowId, {
          tool: ev.tool ?? "tool",
          summary: ev.summary,
          ok: ev.ok,
          durationMs: ev.duration_ms,
          deniedReason: ev.denied_reason || undefined,
          pending: false,
        }),
      groupSeed(gid, ev.at),
    );
  });
  return {
    ...v,
    ...slices,
    nextId: ev.tool_call_id ? slices.nextId : slices.nextId + 1,
  };
}

function activity(v: TaskView, ev: ActivityEvent): TaskView {
  // A straggler after settlement must NOT mint a fresh execution item over a
  // finished turn - drop it; the durable record is complete without it.
  if (settledStraggler(v)) return v;
  // The live micro-facts (ephemeral - absent on replay; the durable record rebuilds
  // the tree minus the live-only rows, the sanctioned delta DESIGN.md names). Three
  // algorithms, three named folds - see each helper for its law.
  if (ev.kind === "tool") return { ...v, ...foldToolActivity(v, ev) };
  if (ev.kind === "model" || ev.kind === "thinking") return { ...v, ...foldRoundActivity(v, ev) };
  return { ...v, ...foldMachineryActivity(v, ev) };
}

function progress(v: TaskView, ev: Ev<"progress">): TaskView {
  // A late heartbeat after settlement would resurrect the live ticker - drop it.
  if (settledStraggler(v)) return v;
  return { ...v, live: { phase: ev.phase, elapsed_s: ev.elapsed_s } };
}

export const MESSAGE_FOLDS: FoldTable<
  | "message.start"
  | "message.delta"
  | "message.end"
  | "step.narration"
  | "tool.called"
  | "activity"
  | "progress"
> = {
  "message.start": messageStart,
  "message.delta": messageDelta,
  "message.end": messageEnd,
  "step.narration": stepNarration,
  "tool.called": toolCalled,
  activity,
  progress,
};
