/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The words a verification receipt wears: pure copy over the server's report, no state.
// The server names a judge only when a model answered (`verifier_model`); a null there means
// no model judged, and the receipt says exactly that - never a guess at who else looked.

type Judged = {
  verifier_model?: string | null;
  verifier_family?: string | null;
  producer_family: string;
};

/** A model id as step rows spell it: the ledger's `provider:model` without the provider prefix. */
export function bareModel(id: string): string {
  const i = id.indexOf(":");
  return i === -1 ? id : id.slice(i + 1);
}

/** Who judged, as the receipt's "Verified by" cell reads: a name and one quiet line under it. */
export function judgeCell(v: Judged): { name: string; note: string } {
  if (!v.verifier_model) {
    return { name: "No model judge", note: "Deterministic checks only" };
  }
  const cross = v.verifier_family && v.verifier_family !== v.producer_family;
  const note = [v.verifier_family, cross ? "cross-family" : null].filter(Boolean).join(" · ");
  return { name: bareModel(v.verifier_model), note };
}

/** The collapsed receipt row's muted line: the suite and who judged. */
export function receiptSummary(v: Judged & { suite: string }): string {
  if (!v.verifier_model) return `${v.suite} · no model judge`;
  const cross = v.verifier_family && v.verifier_family !== v.producer_family;
  return `${v.suite} · judged by ${bareModel(v.verifier_model)}${cross ? " (another model family)" : ""}`;
}

/** Longer than this and a check's text is folded behind "Show details". */
export const FOLD_AT = 200;

/** Whether a check carries a wall of text (the grounding check lists every claim). */
export function isLongCheck(c: { detail?: string | null; actual?: string | null }): boolean {
  return (c.detail?.length ?? 0) > FOLD_AT || (c.actual?.length ?? 0) > FOLD_AT;
}

/** The first line of a long detail: its leading segment (the checks join their parts with
 *  " · " and put the counts first), cut to the fold when that segment alone is still long. */
export function checkSummary(detail: string): string {
  const head = detail.split(" · ")[0];
  if (head.length <= FOLD_AT) return head;
  return `${head.slice(0, head.lastIndexOf(" ", FOLD_AT) > 0 ? head.lastIndexOf(" ", FOLD_AT) : FOLD_AT)}…`;
}
