/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// A conversation's open gate as the task docks draw it: the API's GateOut folded into the
// GateView the shared gate card reads. Pure: no fetch, no DOM.

import type { components } from "~/lib/api/types.gen";
import type { GateView } from "~/lib/task-view";

type GateOut = components["schemas"]["GateOut"];

export function toGateView(g: GateOut): GateView {
  return {
    gate_id: g.id,
    title: g.title ?? undefined,
    kind: g.kind,
    reason: g.reason ?? undefined,
    connector_slug: g.connector_slug || undefined,
    host: g.host || undefined,
    scopes: g.scopes,
    asks_because: g.asks_because,
    expires_at: g.expires_at ?? undefined,
    machine_driver: g.machine_driver ?? null,
    questions: g.questions?.questions ?? [],
  };
}
