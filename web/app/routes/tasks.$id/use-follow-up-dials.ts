/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useMemo, useState } from "react";
import type { ConnectorOut } from "~/components/composer/connectors-control";
import {
  fleetEligible,
  type DeliverableChoice,
  type ModelOut,
  type TaskScale,
} from "~/components/composer/dials";
import { fetchCatalog, type CatalogEntry } from "~/components/connectors/catalog";
import { suggestConnector } from "~/components/connectors/suggest";
import { useConnectFlows } from "~/components/connectors/use-connect-flows";
import { useLoad } from "~/hooks/use-load";
import { api, unwrap } from "~/lib/api/client";
import { useDebouncedValue } from "~/lib/paging";
import { onConnectorsChanged } from "~/lib/settings-bus";
import { followUpDialFields, liveConnectorIds } from "./follow-up-dials";

/**
 * The follow-up box's dials. The box is the HOME box minus the thread's DNA: MODE is inherited
 * from the parent (a fleet conversation continues as a fleet - the API refuses to say
 * otherwise), so there is no mode dial; only a fleet thread loads the model catalog for its
 * Models seats. Size, deliverable and connectors are FRESH-TURN dials: null (or empty) is
 * untouched and stays off the wire, a pick is THIS turn's, and a send clears them so they
 * never read as thread DNA. The catalogs are fetched once the box can mint a follow-up,
 * best-effort: no catalog simply means no dial, never a blocked follow-up.
 */
export function useFollowUpDials({ live, fleetThread }: { live: boolean; fleetThread: boolean }) {
  const [catalog, setCatalog] = useState<ModelOut[] | null>(null);
  const [modelRefs, setModelRefs] = useState<string[]>([]);
  const [connectorCatalog, setConnectorCatalog] = useState<ConnectorOut[] | null>(null);
  const [connectorIds, setConnectorIds] = useState<string[]>([]);
  // The suggestion chip (the landing composer's grammar), fed by the service catalog fetched
  // alongside the connector roster.
  const [serviceCatalog, setServiceCatalog] = useState<CatalogEntry[] | null>(null);
  const [draft, setDraft] = useState("");
  const debouncedDraft = useDebouncedValue(draft, 250);
  const [dismissedSlugs, setDismissedSlugs] = useState<string[]>([]);
  // The size dial (the CLI's --size): any pick - including Auto, which asks planning to
  // re-read the new message - is THIS turn's dial.
  const [turnScale, setTurnScale] = useState<TaskScale | null>(null);
  // The deliverable dial: the wire has no "auto", so a pick of Auto and untouched are the same
  // fact: null.
  const [turnDeliverable, setTurnDeliverable] = useState<DeliverableChoice | null>(null);

  useEffect(() => {
    if (debouncedDraft.trim() === "") setDismissedSlugs([]);
  }, [debouncedDraft]);
  const suggestion = useMemo(() => {
    const hit = suggestConnector(
      debouncedDraft,
      serviceCatalog,
      connectorCatalog ?? [],
      connectorIds,
    );
    return hit && !dismissedSlugs.includes(hit.slug) ? hit : null;
  }, [debouncedDraft, serviceCatalog, connectorCatalog, connectorIds, dismissedSlugs]);
  const suggestFlows = useConnectFlows();

  useLoad(
    live || !fleetThread || catalog !== null
      ? null
      : (signal) => unwrap(api.GET("/v1/models", { signal })),
    [live, fleetThread, catalog],
    {
      onData: (models) => {
        const eligible = fleetEligible(models);
        setCatalog(eligible);
        setModelRefs(eligible.map((m) => m.ref));
      },
      onError: () => setCatalog([]),
    },
  );
  const rosterDue = !live && connectorCatalog === null;
  useLoad(rosterDue ? () => fetchCatalog() : null, [live, connectorCatalog], {
    onData: setServiceCatalog,
  });
  useLoad(
    rosterDue
      ? (signal) => unwrap(api.GET("/v1/connectors", { params: { query: { limit: 200 } }, signal }))
      : null,
    [live, connectorCatalog],
    {
      onData: (page) => {
        setConnectorCatalog(page.items);
        setConnectorIds((cur) => liveConnectorIds(cur, page.items));
      },
      onError: () => setConnectorCatalog([]),
    },
  );
  // Settings announces roster changes on the bus; dropping the catalog re-arms the fetch
  // above, so the dial reflects a connector added/authorized/removed mid-visit.
  useEffect(() => onConnectorsChanged(() => setConnectorCatalog(null)), []);

  const attachConnector = (id: string) => setConnectorIds((cur) => [...cur, id]);
  return {
    fleetThread,
    catalog,
    modelRefs,
    setModelRefs,
    connectorCatalog,
    connectorIds,
    setConnectorIds,
    attachConnector,
    suggestion,
    suggestFlows,
    dismissSuggestion: (slug: string) => setDismissedSlugs((cur) => [...cur, slug]),
    turnScale,
    setTurnScale,
    turnDeliverable,
    setTurnDeliverable,
    /** The box's text as the suggestion chip reads it. */
    draft,
    setDraft,
    /** This turn's dial fields for the follow-up body. */
    wireFields: () =>
      followUpDialFields({
        fleetThread,
        catalog,
        modelRefs,
        connectorIds,
        turnScale,
        turnDeliverable,
      }),
    /** After a send: the next turn starts untouched. */
    clearTurn: () => {
      setConnectorIds([]);
      setTurnScale(null);
      setTurnDeliverable(null);
    },
  };
}

export type FollowUpDials = ReturnType<typeof useFollowUpDials>;
