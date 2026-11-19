/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The ledger and memory folds: spend per step and in total, what the task was told (the context
// ledger), and what it or its bot remembered afterwards.

import { planGroupSeed, stepGroupSeed, upsertGroup, withExec } from "./task-view-exec";
import { marker, withEpilogue } from "./task-view-feed";
import type { Ev, FoldTable, TaskView } from "./task-view-types";
import { isModelCall, isSearch, recordInLedger, splitTokens, withModel } from "./usage-ledger";

/** Each ledger row feeds its step's group twice over: model attribution (the first
 *  frame stamps modelRef - fleet seats read "served by X") and the PER-STEP ledger
 *  (model calls · searches · tokens · cost accumulate on the group, so "what did this step cost" is
 *  answered in place, not only in the topbar total). */
function usageRecorded(v: TaskView, ev: Ev<"usage.recorded">): TaskView {
  const model = ev.model;
  const stepId = ev.step_id;
  const cost = Number(ev.cost_usd) || 0;
  const { tokens, cached } = splitTokens(ev.units);
  const search = isSearch(ev.units);
  // Machine time is a ledger row too: it costs, but it is neither a search nor a model call.
  const call = isModelCall(ev.units);
  const attributed =
    stepId && v.execId != null
      ? withExec(v, stepId, (item) =>
          item.groups.some((g) => g.id === stepId)
            ? upsertGroup(
                item,
                stepId,
                (g) => ({
                  ...g,
                  // A search engine is not a model: only model calls name one.
                  modelRef: g.modelRef ?? (call ? model || undefined : undefined),
                  models: call ? withModel(g.models, model) : g.models,
                  calls: (g.calls ?? 0) + (call ? 1 : 0),
                  searches: (g.searches ?? 0) + (search ? 1 : 0),
                  costUsd: (g.costUsd ?? 0) + cost,
                  tokens: (g.tokens ?? 0) + tokens,
                  cachedTokens: (g.cachedTokens ?? 0) + cached,
                }),
                stepGroupSeed(stepId, {}),
              )
            : item,
        )
      : null;
  return {
    ...v,
    ...(attributed ?? {}),
    usageCalls: v.usageCalls + (call ? 1 : 0),
    usageSearches: v.usageSearches + (search ? 1 : 0),
    usageLedger: recordInLedger(v.usageLedger, { model, stepId, usd: cost, units: ev.units }),
    // cost_usd crosses the wire as a decimal STRING - sum it or the header lies.
    usageUsd: (v.usageUsd ?? 0) + cost,
    // Spend after Delivered: the receipt's (the judge runs once the answer is out). Held per
    // step until that step's `verification.signed` claims it.
    lateUsd:
      v.epilogueId != null && stepId
        ? { ...v.lateUsd, [stepId]: (v.lateUsd[stepId] ?? 0) + cost }
        : v.lateUsd,
  };
}

/** The context ledger's spend record - what this task was told and what it cost in
 *  tokens. The rail keeps the full lane detail; the TREE gets the one-line fact in
 *  the plan group ("Gathered context - 1,028 tok · 4 lanes, 1 trimmed"), because
 *  "what does it already know" is part of the execution story. */
function contextAssembled(v: TaskView, ev: Ev<"context.assembled">): TaskView {
  const trimmed = ev.lanes.filter((l) => l.truncated).length;
  const lanes = ev.lanes.length;
  const rowId = `ctx${v.nextId}`;
  // No row-id guard here: dedup is `seq` at the reducer door - a frame that reaches
  // this fold is new by construction, and `rowId` is minted fresh either way.
  const slices = withExec(v, "plan", (item) =>
    upsertGroup(
      item,
      "plan",
      (g) => ({
        ...g,
        rows: [
          ...g.rows,
          {
            t: "tool",
            id: rowId,
            tool: "engine",
            summary:
              `Gathered context - ${ev.total_tokens} tok · ${lanes} lane${lanes === 1 ? "" : "s"}` +
              (trimmed > 0 ? `, ${trimmed} trimmed` : ""),
            ok: true,
            pending: false,
          },
        ],
      }),
      planGroupSeed("running", undefined, ev.at),
    ),
  );
  return {
    ...v,
    ...slices,
    nextId: slices.nextId + 1,
    context: { lanes: ev.lanes, totalTokens: ev.total_tokens },
  };
}

/** Only when something was actually kept - count 0 is the archivist's recorded
 *  silence and earns no chrome. What WAS kept is elaborated in the epilogue: each
 *  remembered sentence, in full, not a count ("elaborate on the things that are
 *  remembered"). The marker survives only for epilogue-less replays. */
function memoryWritten(v: TaskView, ev: Ev<"memory.written">): TaskView {
  if (ev.count === 0) return v;
  const items = (ev.items ?? []).map((m) => ({
    kind: m.kind,
    text: m.text,
  }));
  const folded = withEpilogue(v, (item) => ({
    ...item,
    remembered: [...item.remembered, ...items],
  }));
  if (folded) return { ...v, feed: folded.feed };
  const first = items[0]?.text;
  const text = ev.count === 1 && first ? `Remembered: ${first}` : `Remembered ${ev.count} things`;
  return {
    ...v,
    feed: marker(v, "muted", "memory", text),
    nextId: v.nextId + 1,
  };
}

/** The DM keeper learned something - a quiet marker in the bot's own name.
 *  Count 0 is the curator's recorded silence and earns no chrome, like the archivist's. */
function botMemoryWritten(v: TaskView, ev: Ev<"bot_memory.written">): TaskView {
  if (ev.count === 0) return v;
  const first = (ev.items ?? [])[0]?.text;
  const text =
    ev.count === 1 && first
      ? `${ev.bot} learned: ${first}`
      : `${ev.bot} learned ${ev.count} things`;
  return {
    ...v,
    feed: marker(v, "muted", "memory", text),
    nextId: v.nextId + 1,
  };
}

export const USAGE_FOLDS: FoldTable<
  "usage.recorded" | "context.assembled" | "memory.written" | "bot_memory.written"
> = {
  "usage.recorded": usageRecorded,
  "context.assembled": contextAssembled,
  "memory.written": memoryWritten,
  "bot_memory.written": botMemoryWritten,
};
