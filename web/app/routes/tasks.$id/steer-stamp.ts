/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The steer's stamp (ADR-064), as the composer's box changes - pure, so the page keeps it in a
 *  ref and the rules are tested here.
 *
 *  - `epoch`: the plan epoch this screen showed when typing BEGAN (the steer's `issued_epoch`);
 *  - `commandId`: one per draft, so a resend of the same words is idempotent;
 *  - `sent`: the words that command id was last sent with. A send whose answer was lost keeps the
 *    id, so resending the same words returns the same row; edited words are a NEW command, or
 *    the server would answer the old row, old words and all. */

import { ApiError } from "~/lib/api/client";

export type SteerStamp = {
  epoch: number | null;
  commandId: string | null;
  sent: string | null;
};

export const NO_STAMP: SteerStamp = {
  epoch: null,
  commandId: null,
  sent: null,
};

/** The stamp after the box reads `text`. `refused` is the words the stale-steer strip offers
 *  (null when there is no strip), `epoch` the one this screen shows now. */
export function restamp(
  stamp: SteerStamp,
  text: string,
  { refused, epoch, mint }: { refused: string | null; epoch: number; mint: () => string },
): SteerStamp {
  const words = text.trim();
  // an emptied box starts over: no stamp, and nothing stale left to offer
  if (words === "") return NO_STAMP;
  // The refused words were edited: their author has read what changed, so the draft is stamped
  // afresh - its next send is an ordinary one, against the epoch this screen shows now.
  if (refused !== null && refused.trim() !== words) return { epoch, commandId: mint(), sent: null };
  // Words edited after a send attempt are another steer: a new command, the same typing epoch.
  if (stamp.sent !== null && stamp.sent.trim() !== words)
    return { ...stamp, commandId: mint(), sent: null };
  if (stamp.epoch === null) return { epoch, commandId: mint(), sent: null };
  return stamp;
}

/** The stamp a send goes out under: the draft's command id (minted now if the box never
 *  stamped one) and the words it carries. */
export function stampForSend(stamp: SteerStamp, words: string, mint: () => string): SteerStamp {
  return { ...stamp, commandId: stamp.commandId ?? mint(), sent: words };
}

/** Where a refused steer says so. A stale one draws the strip above the box, but the strip shows
 *  only while the box holds those words: a refusal whose words are not in the box (a "Send as
 *  steer" tapped after the person typed on) is a toast, never nothing. */
export function refusedSteerSurface(e: unknown, words: string, box: string): "strip" | "toast" {
  return e instanceof ApiError && e.code === "stale_steer" && box.trim() === words.trim()
    ? "strip"
    : "toast";
}
