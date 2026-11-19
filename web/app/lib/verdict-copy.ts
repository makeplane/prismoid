/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The words a task view wears for an answer and a verification receipt: pure copy, no state.
// task-view.ts re-exports these, so every importer keeps its path.

import type { Verdict } from "~/lib/api/events";

/** A stored answer, worn the way the user chose it: option VALUES map back to their
 *  labels, booleans say Yes/No, multi-selects join - free text passes through. */
export function answerLabel(
  q: { type: string; options?: { value: string; label: string }[] },
  value: string | number | boolean | string[],
): string {
  const label = (v: string) => q.options?.find((o) => o.value === v)?.label ?? v;
  if (Array.isArray(value)) return value.map(label).join(", ");
  if (typeof value === "boolean" || q.type === "boolean")
    return value === true || value === "true" ? "Yes" : "No";
  return label(String(value));
}

// ── the verdict voice ─────────────────────────────────────────────────────────────────────────

/** The receipt's DETAIL line - "Verified · document-suite · judged by google". ONE home
 *  for the verdict vocabulary: the in-tree note, the epilogue, and the legacy marker all
 *  speak through these two functions (a wording change used to be three edits and three
 *  re-pinned tests). */
export function verdictLine(
  verdict: Verdict,
  opts: { suite?: string; family?: string | null } = {},
): string {
  const head =
    verdict === "pass" ? "Verified" : verdict === "fail" ? "Verification failed" : "Unverified";
  return [head, opts.suite, opts.family ? `judged by ${opts.family}` : null]
    .filter(Boolean)
    .join(" · ");
}

/** The caveat under "Delivered" when the receipt failed: the answer WAS delivered (verification
 *  never blocks it, ADR-019), so this asks for care, not alarm. */
export function failedCaveat(): string {
  return "Checks found problems. Read it with care.";
}

/** …and the standalone SENTENCE: where to look when the receipt is not clean. A pass
 *  reads as the detail line; fail/unverified point at the artifact instead. */
export function verdictSentence(
  verdict: Verdict,
  opts: { suite?: string; family?: string | null } = {},
): string {
  if (verdict === "fail") return "Verification failed - the receipt is on the artifact";
  if (verdict === "unverified") return "Unverified - nothing could examine it";
  return verdictLine(verdict, opts);
}
