/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Per-tool policy vocabulary (ADR-054), pure. The permissions screen renders exactly what
// the wire says (`policy` is the server-derived EFFECTIVE value, `policy_source` says
// default vs override) - nothing here re-derives the dial, so the one place it lives stays
// the backend's pure function.

import type { components } from "~/lib/api/types.gen";

export type ToolPolicy = components["schemas"]["ToolPolicy"];
export type ConnectorToolOut = components["schemas"]["ConnectorToolOut"];

/** Total over the generated union - a new policy value is a compile error here, never a
 *  silent unlabeled chip. */
export const POLICY_META: Record<ToolPolicy, { label: string; blurb: string }> = {
  allow: { label: "Allow", blurb: "Runs without asking" },
  ask: { label: "Ask", blurb: "Pauses for your approval each time" },
  off: { label: "Off", blurb: "The model never sees this tool" },
};

export const POLICY_ORDER: readonly ToolPolicy[] = ["off", "ask", "allow"];

export type PolicyRow = {
  name: string;
  description: string;
  destructive: boolean;
  policy: ToolPolicy;
  overridden: boolean;
};

/** Read-only vs write grouping, from the probed MCP annotations. `read_only: null` (the
 *  server declared nothing) groups with WRITE - the same safe direction the backend's
 *  default dial takes; destructive never hides among reads. */
export function policyGroups(tools: ConnectorToolOut[]): {
  readOnly: PolicyRow[];
  write: PolicyRow[];
} {
  const rows = tools.map((t): PolicyRow => ({
    name: t.name,
    description: t.description ?? "",
    destructive: t.destructive === true,
    policy: t.policy,
    overridden: t.policy_source === "override",
  }));
  return {
    readOnly: rows.filter((r) => tools.find((t) => t.name === r.name)?.read_only === true),
    write: rows.filter((r) => tools.find((t) => t.name === r.name)?.read_only !== true),
  };
}

/** What a group's bulk trio shows: one value when every row agrees, "mixed" otherwise. */
export function groupSummary(rows: PolicyRow[]): ToolPolicy | "mixed" {
  if (rows.length === 0) return "mixed";
  const first = rows[0].policy;
  return rows.every((r) => r.policy === first) ? first : "mixed";
}

/** The PATCH body after one tri-state click: the WHOLE overrides map (replace semantics,
 *  the wire's contract) - every currently-overridden tool plus this change. Setting a tool
 *  back to its server default would need the default's identity, which the wire deliberately
 *  keeps server-side; an explicit override matching the default is harmless and honest. */
export function withPolicy(
  tools: ConnectorToolOut[],
  name: string,
  value: ToolPolicy,
): Record<string, ToolPolicy> {
  const out: Record<string, ToolPolicy> = {};
  for (const t of tools) if (t.policy_source === "override") out[t.name] = t.policy;
  out[name] = value;
  return out;
}

/** Bulk-set one group: every named row becomes `value`; overrides outside the group stand. */
export function withGroupPolicy(
  tools: ConnectorToolOut[],
  names: string[],
  value: ToolPolicy,
): Record<string, ToolPolicy> {
  const out: Record<string, ToolPolicy> = {};
  for (const t of tools) if (t.policy_source === "override") out[t.name] = t.policy;
  for (const name of names) out[name] = value;
  return out;
}
