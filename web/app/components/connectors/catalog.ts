/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The catalog wire adapter (ADR-054) - ONE file that knows the endpoint shapes, so field
// drift lands here and nowhere else. Pure merge/filter logic exported for tests; fetches
// best-effort (`null` on failure) so surfaces render their roster even when the catalog
// endpoint is unreachable.

import type { components } from "~/lib/api/types.gen";
import { api, unwrap } from "~/lib/api/client";
import type { ConnectorRow } from "./vocabulary";

export type CatalogEntry = components["schemas"]["CatalogEntryOut"];
export type ToolPolicy = components["schemas"]["ToolPolicy"];

export const fetchCatalog = (): Promise<CatalogEntry[] | null> =>
  unwrap(api.GET("/v1/connectors/catalog"))
    .then((out) => out.items)
    .catch(() => null);

/** One card on the /connectors grid: a roster row (connected, whatever its state), or a
 *  catalog entry not yet connected. A connected catalog slug renders ONCE - the roster row
 *  wins (it carries the live truth); custom-URL rows still earn cards. */
export type ConnectorCard =
  | { kind: "mine"; row: ConnectorRow; entry: CatalogEntry | null }
  | { kind: "offered"; entry: CatalogEntry };

export function mergeCatalog(
  catalog: CatalogEntry[] | null,
  roster: ConnectorRow[],
): ConnectorCard[] {
  const bySlug = new Map((catalog ?? []).map((e) => [e.slug, e]));
  const taken = new Set(roster.map((r) => r.catalog_slug).filter(Boolean));
  const mine: ConnectorCard[] = roster.map((row) => ({
    kind: "mine",
    row,
    entry: (row.catalog_slug && bySlug.get(row.catalog_slug)) || null,
  }));
  const offered: ConnectorCard[] = (catalog ?? [])
    .filter((e) => !taken.has(e.slug))
    .map((entry) => ({ kind: "offered", entry }));
  return [...mine, ...offered];
}

export type CardFilter = "all" | "connected" | "available";

export function filterCards(
  cards: ConnectorCard[],
  { filter, category, q }: { filter: CardFilter; category: string | null; q: string },
): ConnectorCard[] {
  const needle = q.trim().toLowerCase();
  return cards.filter((card) => {
    if (filter === "connected" && card.kind !== "mine") return false;
    if (filter === "available" && card.kind !== "offered") return false;
    if (category && card.entry?.category !== category) return false;
    if (!needle) return true;
    const hay =
      card.kind === "mine"
        ? `${card.row.name} ${card.row.slug} ${card.row.server_url} ${card.entry?.description ?? ""}`
        : `${card.entry.name} ${card.entry.slug} ${card.entry.vendor} ${card.entry.description} ${card.entry.category}`;
    return hay.toLowerCase().includes(needle);
  });
}

export function categoriesOf(catalog: CatalogEntry[] | null): string[] {
  return [...new Set((catalog ?? []).map((e) => e.category))];
}

// ── the mutations (the peek's hands) ──────────────────────────────────────────────────────────

export const saveToolPolicies = (id: string, policies: Record<string, ToolPolicy>) =>
  unwrap(
    api.PATCH("/v1/connectors/{connector_id}/policies", {
      params: { path: { connector_id: id } },
      body: { policies },
    }),
  );

export const setAutoAttach = (id: string, on: boolean) =>
  unwrap(
    api.PATCH("/v1/connectors/{connector_id}", {
      params: { path: { connector_id: id } },
      body: { auto_attach: on },
    }),
  );

export const disableConnector = (id: string) =>
  unwrap(
    api.POST("/v1/connectors/{connector_id}/disable", {
      params: { path: { connector_id: id } },
    }),
  );

export const enableConnector = (id: string) =>
  unwrap(
    api.POST("/v1/connectors/{connector_id}/enable", {
      params: { path: { connector_id: id } },
    }),
  );
