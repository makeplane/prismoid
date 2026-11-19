/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The follow-up box's per-turn dials as the wire takes them, apart from the hook that holds
// them (use-follow-up-dials.ts). Each field rides only when the person moved its dial, so an
// untouched box keeps the thread's own choices.
import {
  splitDeliverableChoice,
  type DeliverableChoice,
  type ModelOut,
  type TaskScale,
} from "~/components/composer/dials";
import type { ConnectorOut } from "~/components/composer/connectors-control";

export type TurnDials = {
  fleetThread: boolean;
  catalog: readonly ModelOut[] | null;
  modelRefs: string[];
  connectorIds: string[];
  turnScale: TaskScale | null;
  turnDeliverable: DeliverableChoice | null;
};

/** The dial fields of one follow-up body. */
export function followUpDialFields(d: TurnDials) {
  return {
    // Send models only when the picker narrowed them - the full fleet is the default.
    ...(d.fleetThread && d.catalog && d.modelRefs.length < d.catalog.length
      ? { models: d.modelRefs }
      : {}),
    // Fresh-turn connectors merge server-side with earlier turns'.
    ...(d.connectorIds.length ? { connectors: d.connectorIds } : {}),
    // THIS turn's size dial: absent = keep the thread's; "auto" = re-resolve from the new
    // message's own numbers.
    ...(d.turnScale !== null ? { scale: d.turnScale } : {}),
    // THIS turn's deliverable dial: absent = the turn re-resolves (fresh kind language
    // re-stamps; silence keeps the thread's path). ONE dial carries kind + document format,
    // split back to the wire's two fields; untouched/Auto sends neither.
    ...(d.turnDeliverable !== null ? splitDeliverableChoice(d.turnDeliverable) : {}),
  };
}

/** Selections the fresh connector catalog still honors (present and live): a stale toggle must
 *  not ride into a 422 at the door. */
export function liveConnectorIds(
  ids: readonly string[],
  catalog: readonly ConnectorOut[],
): string[] {
  return ids.filter((id) => catalog.some((c) => c.id === id && c.status === "live"));
}
