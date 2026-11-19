/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The connector vocabulary (ADR-020) - status tones, labels, verbs, and the flow
// shape, extracted from the settings panel the day a second consumer existed (the /connectors
// page): "typed twice → it's a kit primitive", applied to a domain. Pure values and pure
// functions only; the stateful OAuth dance lives in use-connect-flows.ts beside this.

import type { components } from "~/lib/api/types.gen";
import type { Tone } from "~/components/kit";
import { api, unwrap } from "~/lib/api/client";

export type ConnectorRow = components["schemas"]["ConnectorOut"];
export type ConnectorStatus = ConnectorRow["status"];

export const fetchConnectors = () =>
  unwrap(api.GET("/v1/connectors", { params: { query: { limit: 200 } } }));

export const CONNECTOR_POLL_MS = 2_000;
/** The backend's authorize→callback flow TTL (operations/connectors.FLOW_TTL, 10 min) plus
 *  grace. Normally the server reverts an abandoned `authorizing` row lazily at the TTL and
 *  the poll settles on status - this clock is the client fallback so a watch can never run
 *  forever. */
export const CONNECTOR_FLOW_TTL_MS = 11 * 60_000;

/** Status → tone, the sanctioned per-domain map (never hues for status). Total over the
 *  GENERATED union - a new backend status is a compile error here, not a silent fallback
 *  (the serializer ships the enum precisely so this Record can be exhaustive). */
export const CONNECTOR_TONE: Record<ConnectorStatus, Tone> = {
  live: "success",
  authorizing: "warning",
  pending_auth: "warning",
  needs_reauth: "warning",
  error: "danger",
  disabled: "muted",
};

const CONNECTOR_LABEL: Record<ConnectorStatus, string> = {
  live: "Connected",
  authorizing: "Authorizing…",
  pending_auth: "Needs authorization",
  needs_reauth: "Needs re-authorization",
  error: "Error",
  disabled: "Disabled",
};

/** The status, in words a person would say. */
export const connectorStatusLabel = (status: ConnectorStatus): string => CONNECTOR_LABEL[status];

/** The verb law (capability law): CONNECT belongs to the OAuth dance alone - on a no-auth or
 *  bearer row it would run OAuth discovery against a server with nothing to discover. RETRY
 *  is the probe, offered exactly where a probe can heal: any non-OAuth row that is neither
 *  live nor deliberately disabled. Live and disabled rows offer neither - absent, never
 *  disabled-with-an-excuse. DISABLE belongs to rows that are on; ENABLE to the one that
 *  is not. */
export function connectorVerbs(row: Pick<ConnectorRow, "status" | "auth_kind">): {
  connect: boolean;
  retry: boolean;
  disable: boolean;
  enable: boolean;
} {
  if (row.status === "disabled")
    return { connect: false, retry: false, disable: false, enable: true };
  if (row.status === "live") return { connect: false, retry: false, disable: true, enable: false };
  const connect = row.auth_kind === "oauth";
  return { connect, retry: !connect, disable: true, enable: false };
}

/** One in-flight OAuth watch. `opened: false` means the popup was blocked and the row offers
 *  the consent URL as a link instead; `startedAt` feeds the TTL fallback. */
export type ConnectFlow = {
  phase: "starting" | "waiting";
  url: string | null;
  opened: boolean;
  startedAt: number;
};

/** What one poll answer means for one waiting flow - the watch loop's whole decision, pure:
 *  `keep` (still authorizing, inside the TTL), `expired` (still authorizing past it), `live`,
 *  `failed` (settled on anything else), `gone` (removed while waiting). */
export function settleFlow(
  row: Pick<ConnectorRow, "status"> | undefined,
  flow: ConnectFlow,
  now: number,
): "keep" | "expired" | "live" | "failed" | "gone" {
  if (!row) return "gone";
  if (row.status === "authorizing")
    return now - flow.startedAt < CONNECTOR_FLOW_TTL_MS ? "keep" : "expired";
  return row.status === "live" ? "live" : "failed";
}
