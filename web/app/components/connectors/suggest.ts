/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The composer's suggestion matcher (ADR-054) - the SAME word-boundary grammar the backend's
// auto-attach door speaks (schemas/connector_catalog.suggest), run client-side at zero
// latency: what the chip offers is what the door would attach. The VOCABULARY is not
// re-derived here - the backend ships each row's `suggest_words` (its one recipe: slug + name
// words + catalog keywords + tool-name tokens, generic verbs stoplisted) and this file keeps
// ONLY the boundary match, so the chip and the door can never disagree ("one grammar"). A
// catalog entry that is not yet a row still matches on its `keywords`, the same values.

import type { CatalogEntry } from "./catalog";
import type { ConnectorRow } from "./vocabulary";

const MIN_KEYWORD = 3;

export type ConnectorSuggestion = {
  slug: string;
  name: string;
  /** Present when a LIVE roster row matches - the chip attaches it; absent = catalog-only,
   *  the chip runs the one-click connect first. */
  connectorId: string | null;
  keyword: string;
};

function boundaryHit(text: string, keyword: string): boolean {
  const k = keyword.trim().toLowerCase();
  if (k.length < MIN_KEYWORD) return false;
  const escaped = k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  // `_` is a boundary too (backend parity): "linear" must not match linear_regression_notes.
  return new RegExp(`(?<![a-z0-9_])${escaped}(?![a-z0-9_])`).test(text);
}

/** Per-row vocabulary, straight off the wire - the backend's ONE recipe, never re-derived
 *  here (a second copy would drift from the backend's one grammar). */
function rosterWords(row: ConnectorRow): string[] {
  return row.suggest_words ?? [];
}

function bestKeyword(text: string, words: string[]): string | null {
  let best: string | null = null;
  for (const w of words) {
    if ((best === null || w.length > best.length) && boundaryHit(text, w)) best = w;
  }
  return best;
}

/** ONE suggestion or none (a rack of chips is noise): a LIVE roster match outranks a
 *  catalog-only entry; among roster matches the longest matched keyword wins; already
 *  attached rows are ABSENT, never disabled (the capability law). Matching runs over the
 *  draft's last stretch, lowercased. */
export function suggestConnector(
  text: string,
  catalog: CatalogEntry[] | null,
  roster: ConnectorRow[],
  attachedIds: string[],
): ConnectorSuggestion | null {
  const tail = text.slice(-200).toLowerCase();
  if (!tail.trim()) return null;
  const attached = new Set(attachedIds);

  let rosterBest: ConnectorSuggestion | null = null;
  for (const row of roster) {
    if (row.status !== "live" || attached.has(row.id)) continue;
    const hit = bestKeyword(tail, rosterWords(row));
    if (hit && (rosterBest === null || hit.length > rosterBest.keyword.length))
      rosterBest = { slug: row.slug, name: row.name, connectorId: row.id, keyword: hit };
  }
  if (rosterBest) return rosterBest;

  const connectedSlugs = new Set(roster.map((r) => r.catalog_slug).filter(Boolean));
  let offeredBest: ConnectorSuggestion | null = null;
  for (const entry of catalog ?? []) {
    if (connectedSlugs.has(entry.slug)) continue; // connected-but-not-live rows are theirs to heal
    const hit = bestKeyword(tail, [...entry.keywords]);
    if (hit && (offeredBest === null || hit.length > offeredBest.keyword.length))
      offeredBest = { slug: entry.slug, name: entry.name, connectorId: null, keyword: hit };
  }
  return offeredBest;
}
