/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** THE time-scale law: human units, whole seconds - "45s",
 *  "1m 30s", "2h 1m 15s", "1d 2h 5m 30s" - and DAYS are the ceiling (no weeks, no
 *  months: past days a duration is a date's job). One home for every duration the app
 *  speaks - the ticker, the tree's clocks and settled facts, gate countdowns -
 *  lib-level so components and routes alike may import it. This retired the one-unit
 *  "247s" rule: past a minute the bare count WAS the mental math. Never "4:07" (colon
 *  clocks read as timestamps). */
export function formatSeconds(totalS: number): string {
  if (!Number.isFinite(totalS)) return "0s";
  const t = Math.max(0, Math.floor(totalS));
  const s = t % 60;
  const m = Math.floor(t / 60) % 60;
  const h = Math.floor(t / 3600) % 24;
  const d = Math.floor(t / 86400);
  if (d > 0) return `${d}d ${h}h ${m}m ${s}s`;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

/** Durations read at a glance: sub-10s keeps one decimal ("3.4s" - micro tool calls are
 *  where tenths carry signal); everything longer speaks the one scale law above. The
 *  decimal band is this wrapper's only opinion. Live ticking clocks use `formatSeconds`
 *  directly - a clock jumping tenths is jitter, not signal. */
export function formatDurationMs(ms: number): string {
  if (ms < 10_000) return `${Math.max(0.1, Math.round(ms / 100) / 10)}s`;
  return formatSeconds(Math.round(ms / 1000));
}

/** A count at a glance: 840, 12.8k. */
function compactCount(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`;
}

/** One token figure, one spelling - group receipts, the tail line, and the epilogue
 *  receipt share it (kept out of progress-copy: the peek's epilogue needed
 *  it and components/ must not import route code). `cached` is the cache-read and
 *  cache-write tokens: they are most of what a call costs, so a figure that leaves them
 *  out beside a dollar amount reads as a bug ("22 tok · $0.040"). */
export function formatTokens(n: number, cached = 0): string {
  if (cached <= 0) return `${compactCount(n)} tok`;
  return n > 0
    ? `${compactCount(n)} tok + ${compactCount(cached)} cached`
    : `${compactCount(cached)} cached`;
}

/** The receipt's step count ("1 step", "3 steps"); nothing for a turn that ran none. */
export function formatSteps(n: number): string | null {
  if (n <= 0) return null;
  return `${n} step${n === 1 ? "" : "s"}`;
}

/** A receipt's token figure, cached tokens beside fresh ones; nothing when it carries neither. */
export function formatReceiptTokens(r: { tokens?: number | null; cachedTokens?: number | null }) {
  if (r.tokens == null && r.cachedTokens == null) return null;
  return formatTokens(r.tokens ?? 0, r.cachedTokens ?? 0);
}

/** A receipt's tokens as ONE figure ("52.4k tokens"), fresh and cached together: the at-a-glance
 *  spelling for the end of a turn, where `formatReceiptTokens` (the split) is the detail. */
export function formatReceiptTokenTotal(r: {
  tokens?: number | null;
  cachedTokens?: number | null;
}): string | null {
  if (r.tokens == null && r.cachedTokens == null) return null;
  return `${compactCount((r.tokens ?? 0) + (r.cachedTokens ?? 0))} tokens`;
}
