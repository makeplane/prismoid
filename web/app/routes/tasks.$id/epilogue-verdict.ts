/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// What the end of a turn says about its check, decided here and only drawn by epilogue.tsx: the
// line above the delivery facts and the colour of the rule that ties the two together. Pure.

import { failedCaveat, verdictSentence, type FeedItem } from "~/lib/task-view";

type EpilogueItem = Extract<FeedItem, { kind: "epilogue" }>;

/** `warning` asks for care (problems found, or nothing could check it); `success` is a clean
 *  receipt. */
export type VerdictTone = "warning" | "success";

export type EpilogueVerdict = {
  tone: VerdictTone;
  text: string;
  /** A failed receipt is a caveat the reader must not miss: it is announced as a note. */
  caveat: boolean;
};

/** The verdict line for a turn's end, or null while there is no receipt to speak of: an answer
 *  with nothing to verify, or one whose receipt has not landed. The stream does not say whether
 *  a receipt is owed (a step may publish without one), so "still checking" is never claimed
 *  here; the artifact card's own pending pill is the only place that waits. */
export function epilogueVerdict(item: EpilogueItem): EpilogueVerdict | null {
  if (item.verdict === "fail") return { tone: "warning", text: failedCaveat(), caveat: true };
  if (item.verdict === "pass") {
    const text = verdictSentence("pass", { suite: item.suite, family: item.family });
    return { tone: "success", text, caveat: false };
  }
  if (item.verdict === "unverified") {
    return { tone: "warning", text: verdictSentence("unverified"), caveat: false };
  }
  return null;
}
