/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Artifacts' folds: a publish becomes a card (or a working chip), a revision repoints its
// lineage, the streamed draft grows until its card replaces it, and the verifier's receipt
// lands on the artifact, in its step's group and in the turn's epilogue.

import type { Verdict } from "~/lib/api/events";
import { verdictLine, verdictSentence } from "./verdict-copy";
import { marker, settledStraggler, upsertArtifact, withEpilogue } from "./task-view-feed";
import type {
  Ev,
  ExecRow,
  FeedItem,
  FeedTone,
  FoldTable,
  TaskView,
  Verification,
} from "./task-view-types";

/** A save on an existing lineage: the NEW id becomes the lineage's face.
 *  Swap the shelf entry in place and repoint any feed card at the new id - history stays
 *  reachable through the reader's version picker, not through duplicate cards.
 *
 *  **A revision with nothing to repoint becomes a card.** A later
 *  TURN that rebuilds a prior turn's deliverable emits `revised` - never `produced` -
 *  and this turn's feed starts empty, so the repoint was a no-op and a five-turn task's
 *  final document was invisible in the very turn that made it ("i still don't see the
 *  document"). The lineage's newest face must be visible in the turn it landed. */
function artifactRevised(v: TaskView, ev: Ev<"artifact.revised">): TaskView {
  const known = v.artifacts.some((a) => a.id === ev.supersedes_id);
  if (!known) return revisionCard(v, ev);
  const artifacts = v.artifacts.map((a) =>
    a.id === ev.supersedes_id
      ? {
          ...a,
          id: ev.artifact_id,
          title: ev.title ?? a.title,
          revision: ev.revision,
        }
      : a,
  );
  return {
    ...v,
    artifacts,
    feed: v.feed.map((item) =>
      item.kind === "artifact" && item.artifactId === ev.supersedes_id
        ? {
            ...item,
            artifactId: ev.artifact_id,
            title: ev.title ?? item.title,
          }
        : item,
    ),
  };
}

/** A revision of a lineage this view never saw: a shelf entry and a card of its own. */
function revisionCard(v: TaskView, ev: Ev<"artifact.revised">): TaskView {
  return {
    ...v,
    artifacts: upsertArtifact(v.artifacts, {
      id: ev.artifact_id,
      title: ev.title,
      type: ev.artifact_type,
      revision: ev.revision,
      role: ev.role,
    }),
    feed: [
      ...v.feed,
      {
        kind: "artifact",
        id: `a${v.nextId}`,
        artifactId: ev.artifact_id,
        title: ev.title,
      },
    ],
    nextId: v.nextId + 1,
  };
}

function artifactProduced(v: TaskView, ev: Ev<"artifact.produced">): TaskView {
  const artifacts = upsertArtifact(v.artifacts, {
    id: ev.artifact_id,
    title: ev.title,
    type: ev.artifact_type,
    role: ev.role,
  });
  // A working-role IMAGE is the figure fleet landing mid-run: honest on the
  // record and the shelf, but never a transcript card and never the draft's retirement -
  // a concurrent batch of PNGs must not bury the section the user is reading (review
  // M10; the fold lives HERE, at the reducer, per the house law).
  if (ev.role === "working" && ev.artifact_type === "image") {
    return { ...v, artifacts };
  }
  if (ev.role === "working") return workingChip({ ...v, draft: null, artifacts }, ev);
  // An artifact is a first-class turn in the conversation - a card, not just a marker.
  // The draft it grew from retires here: the card is the settled form of the same text.
  return {
    ...v,
    draft: null,
    artifacts,
    feed: [
      ...v.feed,
      {
        kind: "artifact",
        id: `a${v.nextId}`,
        artifactId: ev.artifact_id,
        title: ev.title,
      },
    ],
    nextId: v.nextId + 1,
  };
}

/** Working artifacts (round six): a CHIP in the trailing chip row, never a card -
 *  fourteen full-width section cards buried the one real deliverable. The draft
 *  still retires; the rail's shelf already carries the full entry. */
function workingChip(v: TaskView, ev: Ev<"artifact.produced">): TaskView {
  const chip = {
    artifactId: ev.artifact_id,
    title: ev.title ?? "artifact",
    type: ev.artifact_type,
  };
  const feed = v.feed.slice();
  const last = feed[feed.length - 1];
  if (last && last.kind === "working-artifacts") {
    const items = last.items.some((c) => c.artifactId === chip.artifactId)
      ? last.items.map((c) => (c.artifactId === chip.artifactId ? chip : c))
      : [...last.items, chip];
    feed[feed.length - 1] = { ...last, items };
    return { ...v, feed };
  }
  feed.push({
    kind: "working-artifacts",
    id: `w${v.nextId}`,
    items: [chip],
  });
  return { ...v, feed, nextId: v.nextId + 1 };
}

/** Durable checkpoint of the document-so-far. Longer wins: the live deltas may already
 *  be ahead of the last snapshot, and a shorter prefix must never eat streamed text. */
function artifactSnapshot(v: TaskView, ev: Ev<"artifact.snapshot">): TaskView {
  if (typeof ev.content !== "string" || !ev.path) return v;
  const cur = v.draft && v.draft.path === ev.path ? v.draft.text : "";
  return ev.content.length > cur.length ? { ...v, draft: { path: ev.path, text: ev.content } } : v;
}

/** The streaming answer. Offsets make frames self-describing: contiguous appends,
 *  idempotent resends overlay, and a dropped frame leaves a visible seam that the
 *  next durable snapshot heals - never silent corruption. */
function draftText(prev: string, ev: Ev<"artifact.delta">): string {
  const off = ev.offset ?? 0;
  if (off === prev.length) return prev + ev.text;
  if (off === 0) return ev.text.length >= prev.length ? ev.text : prev; // whole-current preview
  if (off < prev.length) return prev.slice(0, off) + ev.text;
  return `${prev} … ${ev.text}`;
}

function artifactDelta(v: TaskView, ev: Ev<"artifact.delta">): TaskView {
  // A late delta after settlement would resurrect the retired draft - drop it.
  if (settledStraggler(v)) return v;
  const prev = v.draft && v.draft.path === ev.path ? v.draft.text : "";
  return { ...v, draft: { path: ev.path, text: draftText(prev, ev) } };
}

function verdictTone(verdict: Verdict): FeedTone {
  if (verdict === "pass") return "success";
  return verdict === "fail" ? "danger" : "warning";
}

/** The receipt's DETAIL also lands inside the producing step's group - the tree is
 *  settled by now (the receipt arrives post-delivery), so this targets the
 *  group directly across the whole feed, NEVER through withExec (which would mint a
 *  stray new item after settlement). */
function noteVerdictInStep(
  feed: FeedItem[],
  ev: Ev<"verification.signed">,
  tone: FeedTone,
): FeedItem[] {
  if (!ev.step_id) return feed;
  const note: ExecRow = {
    t: "note",
    id: `vr-${ev.artifact_id ?? ev.seq}`,
    // Amber, never red, inside the tree: the loud verdict lives on
    // the artifact badge where the user acts on it.
    tone: tone === "danger" ? "warning" : tone,
    text: verdictLine(ev.verdict, {
      suite: ev.suite,
      family: ev.verifier_family,
    }),
  };
  return feed.map((item) => {
    if (item.kind !== "execution") return item;
    if (!item.groups.some((g) => g.id === ev.step_id)) return item;
    return {
      ...item,
      groups: item.groups.map((g) =>
        g.id === ev.step_id && !g.rows.some((r) => r.id === note.id)
          ? { ...g, rows: [...g.rows, note] }
          : g,
      ),
    };
  });
}

function verificationSigned(prev: TaskView, ev: Ev<"verification.signed">): TaskView {
  // The spend that landed after delivery on this step IS this receipt's cost.
  const checkUsd = (ev.step_id && prev.lateUsd[ev.step_id]) || 0;
  const lateUsd = Object.fromEntries(
    Object.entries(prev.lateUsd).filter(([id]) => id !== ev.step_id),
  );
  const v: TaskView = { ...prev, lateUsd, verifyUsd: prev.verifyUsd + checkUsd };
  const verification: Verification = {
    verdict: ev.verdict,
    verifier_family: ev.verifier_family,
    artifact_id: ev.artifact_id,
    suite: ev.suite,
    signature: ev.signature,
    ...(checkUsd > 0 ? { costUsd: checkUsd } : {}),
  };
  const artifacts = ev.artifact_id
    ? upsertArtifact(v.artifacts, { id: ev.artifact_id, verdict: ev.verdict })
    : v.artifacts;
  const tone = verdictTone(ev.verdict);
  const feed = noteVerdictInStep(v.feed, ev, tone);
  // The receipt joins the EPILOGUE (the quiet end-of-turn block) when one is live -
  // the normal path, receipt after delivery. A fleet seat verifying MID-run
  // has no epilogue yet and keeps the quiet marker (amber on fail, never red - the
  // artifact badge carries the loud verdict where you act on it).
  const folded = withEpilogue({ ...v, feed }, (item) => ({
    ...item,
    verdict: ev.verdict,
    suite: ev.suite,
    family: ev.verifier_family,
    artifactId: ev.artifact_id,
    ...(checkUsd > 0 ? { checkUsd } : {}),
  }));
  if (folded) return { ...v, verification, artifacts, feed: folded.feed };
  return {
    ...v,
    verification,
    artifacts,
    feed: marker(
      { ...v, feed },
      tone === "danger" ? "warning" : tone,
      ev.verdict === "pass" ? "verified" : "failed",
      verdictSentence(ev.verdict),
    ),
    nextId: v.nextId + 1,
  };
}

/** Accepted onto the record; the tree/epilogue rendering ("QA review - 3
 *  findings, revising") is the named web follow-up. Replay must never crash on it. */
function qaReviewed(v: TaskView): TaskView {
  return v;
}

export const ARTIFACT_FOLDS: FoldTable<
  | "artifact.revised"
  | "artifact.produced"
  | "artifact.snapshot"
  | "artifact.delta"
  | "verification.signed"
  | "qa.reviewed"
> = {
  "artifact.revised": artifactRevised,
  "artifact.produced": artifactProduced,
  "artifact.snapshot": artifactSnapshot,
  "artifact.delta": artifactDelta,
  "verification.signed": verificationSigned,
  "qa.reviewed": qaReviewed,
};
