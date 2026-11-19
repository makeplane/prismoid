/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The usage ledger as the page reads it: what one `usage.recorded` frame means (a model
// call or a web search, fresh tokens or cached ones), and the per-task breakdown the
// Models popover draws. Pure - the fold in task-view-usage.ts feeds it, components format.

import type { ExecGroup, TaskView } from "./task-view-types";

/** Native units the provider bills at the cache's rates - most of what a long prompt costs. */
const CACHED_UNITS = [
  "cache_read_token",
  "cache_write_token",
  "cache_write_1h_token",
  "flex_cache_read_token",
] as const;

/** Native units for the prompt and the reply themselves. */
const FRESH_UNITS = [
  "input_token",
  "output_token",
  "flex_input_token",
  "flex_output_token",
] as const;

type Units = Readonly<Record<string, number>> | null | undefined;

const sum = (units: Units, keys: readonly string[]) =>
  keys.reduce((n, k) => n + (units?.[k] ?? 0), 0);

/** The tokens a call read and wrote fresh, and the ones it took from or put in the cache. */
export function splitTokens(units: Units): { tokens: number; cached: number } {
  return { tokens: sum(units, FRESH_UNITS), cached: sum(units, CACHED_UNITS) };
}

/** A web search is billed per query (`search`), a model call per token: they are counted apart. */
export function isSearch(units: Units): boolean {
  return (units?.search ?? 0) > 0;
}

/** Machine time is billed per second (`second`): a ledger row, but not a model call. */
export function isMachineTime(units: Units): boolean {
  return (units?.second ?? 0) > 0;
}

/** Only a row that is neither a search nor Machine time is a model call. */
export function isModelCall(units: Units): boolean {
  return !isSearch(units) && !isMachineTime(units);
}

/** "8 model calls · 2 searches" - the one spelling for the header, the rail, a step row and
 *  the popover. A search is not a model call, so it never inflates the first figure. */
export function callsLabel(calls: number, searches: number): string {
  const bits: string[] = [];
  if (calls > 0 || searches === 0) bits.push(`${calls} model call${calls === 1 ? "" : "s"}`);
  if (searches > 0) bits.push(`${searches} search${searches === 1 ? "" : "es"}`);
  return bits.join(" · ");
}

/** Add a model to a step's list of models, in the order it first appeared. */
export function withModel(models: readonly string[] | undefined, model: string): string[] {
  const seen = models ?? [];
  return !model || seen.includes(model) ? [...seen] : [...seen, model];
}

/** The models a step names: every one its ledger rows used, else the one it was planned with. */
export function modelsOf(g: Pick<ExecGroup, "models" | "modelRef">): string[] {
  return g.models?.length ? g.models : g.modelRef ? [g.modelRef] : [];
}

/** The whole task's ledger, folded off `usage.recorded` frames beside the header's spend. */
export type UsageLedger = {
  byModel: Record<string, { calls: number; searches: number; seconds: number; usd: number }>;
  byStep: Record<string, number>;
  units: Record<string, number>;
};

export const EMPTY_LEDGER: UsageLedger = { byModel: {}, byStep: {}, units: {} };

/** One frame onto the ledger. */
export function recordInLedger(
  ledger: UsageLedger,
  row: { model: string; stepId?: string | null; usd: number; units: Units },
): UsageLedger {
  const prev = ledger.byModel[row.model] ?? { calls: 0, searches: 0, seconds: 0, usd: 0 };
  const search = isSearch(row.units);
  const units = { ...ledger.units };
  for (const [k, n] of Object.entries(row.units ?? {})) units[k] = (units[k] ?? 0) + n;
  return {
    byModel: {
      ...ledger.byModel,
      [row.model]: {
        calls: prev.calls + (isModelCall(row.units) ? 1 : 0),
        searches: prev.searches + (search ? 1 : 0),
        seconds: prev.seconds + (row.units?.second ?? 0),
        usd: prev.usd + row.usd,
      },
    },
    byStep: row.stepId
      ? { ...ledger.byStep, [row.stepId]: (ledger.byStep[row.stepId] ?? 0) + row.usd }
      : ledger.byStep,
    units,
  };
}

export type UsageBreakdown = {
  models: { model: string; calls: number; searches: number; seconds: number; usd: number }[];
  /** Each step by its title; spend with no step (naming, memory) is the last row. */
  steps: { id: string; title: string; usd: number }[];
  units: [string, number][];
};

const byUsd = <T extends { usd: number }>(a: T, b: T) => b.usd - a.usd;

/** What the Models popover draws: models and steps by what they cost, steps by TITLE. */
export function usageBreakdown(
  view: Pick<TaskView, "usageLedger" | "usageUsd" | "feed">,
): UsageBreakdown {
  const { byModel, byStep, units } = view.usageLedger;
  const titles = new Map<string, string>();
  for (const item of view.feed) {
    if (item.kind !== "execution") continue;
    for (const g of item.groups) if (g.kind === "step" && g.title) titles.set(g.id, g.title);
  }
  const steps = Object.entries(byStep)
    .map(([id, usd]) => ({ id, title: titles.get(id) ?? "Earlier step", usd }))
    .sort(byUsd);
  const unattributed = (view.usageUsd ?? 0) - steps.reduce((n, s) => n + s.usd, 0);
  // Rounding dust is not a row: only a real share of the spend names itself.
  if (unattributed > 0.0005) {
    steps.push({ id: "", title: "Not tied to a step", usd: unattributed });
  }
  return {
    models: Object.entries(byModel)
      .map(([model, m]) => ({ model, ...m }))
      .sort(byUsd),
    steps,
    units: Object.entries(units),
  };
}
