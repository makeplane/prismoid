/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The OAuth connect dance, extracted VERBATIM in skeleton from the settings panel the day
// the /connectors page became its second consumer. The subtleties are the point
// and they are documented where they live:
//
// - The tab is claimed SYNCHRONOUSLY inside the click gesture (past the browser's
//   transient-activation window a window.open is silently blocked), aimed once the URL
//   arrives, `opener` severed (reverse-tabnabbing).
// - One poll while any flow waits, skipped while `document.hidden`, each flow settling
//   independently; the TTL fallback means a watch can never pin "Waiting…" forever.
// - `connectFromCatalog` keys the flow by SLUG until the create answers with the row id,
//   then re-keys - the tab is claimed before the id exists.

import { useCallback, useEffect, useState } from "react";
import { toast } from "@prismoid/blocks/toast";
import { api, toastError, unwrap } from "~/lib/api/client";
import { emitConnectorsChanged } from "~/lib/settings-bus";
import { CONNECTOR_POLL_MS, type ConnectFlow, type ConnectorRow, settleFlow } from "./vocabulary";

export function useConnectFlows({
  onRows,
}: {
  /** Fresh roster from each poll tick - the caller folds it into its own state. */
  onRows?: (rows: ConnectorRow[]) => void;
} = {}) {
  const [flows, setFlows] = useState<Record<string, ConnectFlow>>({});

  const dropFlow = useCallback((key: string) => {
    setFlows((cur) => {
      if (!(key in cur)) return cur;
      const next = { ...cur };
      delete next[key];
      return next;
    });
  }, []);

  /** The two-step dance on an EXISTING row: claim tab → mint URL → aim → watch. */
  const connectExisting = useCallback(
    async (id: string) => {
      if (flows[id]) return; // one flow per row - a double-click must not mint a second state
      const tab = window.open("about:blank", "_blank");
      if (tab) tab.opener = null;
      setFlows((cur) => ({
        ...cur,
        [id]: { phase: "starting", url: null, opened: tab !== null, startedAt: Date.now() },
      }));
      try {
        const { authorize_url } = await unwrap(
          api.POST("/v1/connectors/{connector_id}/authorize", {
            params: { path: { connector_id: id } },
            // The answer sets the HttpOnly cookie that binds the sign-in to this browser; the
            // callback refuses any other one. A cross-origin answer sets it only with credentials.
            credentials: "include",
          }),
        );
        if (tab) tab.location.replace(authorize_url);
        setFlows((cur) => ({
          ...cur,
          [id]: {
            phase: "waiting",
            url: authorize_url,
            opened: tab !== null,
            startedAt: Date.now(),
          },
        }));
      } catch (e) {
        tab?.close();
        dropFlow(id);
        toastError(e, "Couldn't start the connection.");
      }
    },
    [flows, dropFlow],
  );

  /** One-click connect: create-from-catalog + begin-authorization in one round
   *  trip. Returns the created row (whatever its status) so the caller can refresh. */
  const connectFromCatalog = useCallback(
    async (slug: string): Promise<ConnectorRow | null> => {
      if (slug in flows) return null;
      const tab = window.open("about:blank", "_blank");
      if (tab) tab.opener = null;
      setFlows((cur) => ({
        ...cur,
        [slug]: { phase: "starting", url: null, opened: tab !== null, startedAt: Date.now() },
      }));
      try {
        const { connector, authorize_url } = await unwrap(
          api.POST("/v1/connectors/connect", {
            body: { catalog_slug: slug },
            credentials: "include", // the browser-binding cookie, as above
          }),
        );
        if (!authorize_url) {
          // Live already (an open server), or an error the row wears - nothing to consent to.
          tab?.close();
          dropFlow(slug);
          if (connector.status === "live") toast.success(`${connector.name} connected.`);
          else toast.error(connector.last_error ?? "The connector could not start authorization.");
          emitConnectorsChanged();
          return connector;
        }
        if (tab) tab.location.replace(authorize_url);
        // Re-key: the watch matches rows by ID, and now there is one.
        setFlows((cur) => {
          const next = { ...cur };
          delete next[slug];
          next[connector.id] = {
            phase: "waiting",
            url: authorize_url,
            opened: tab !== null,
            startedAt: Date.now(),
          };
          return next;
        });
        emitConnectorsChanged();
        return connector;
      } catch (e) {
        tab?.close();
        dropFlow(slug);
        toastError(e, "Couldn't connect.");
        return null;
      }
    },
    [flows, dropFlow],
  );

  // The watch: poll while any flow waits; settle each row the moment its status moves off
  // `authorizing` - or at the TTL fallback (settleFlow is the pure decision).
  const watching = Object.values(flows).some((f) => f.phase === "waiting");
  useEffect(() => {
    if (!watching) return;
    let alive = true;
    const every = window.setInterval(() => {
      if (document.hidden) return;
      void api.GET("/v1/connectors", { params: { query: { limit: 200 } } }).then(({ data }) => {
        if (!alive || !data) return;
        onRows?.(data.items);
        for (const [key, flow] of Object.entries(flows)) {
          if (flow.phase !== "waiting") continue;
          const row = data.items.find((c) => c.id === key);
          const verdict = settleFlow(row, flow, Date.now());
          if (verdict === "keep") continue;
          dropFlow(key);
          if (verdict === "expired") toast.error("The authorization didn't complete.");
          else if (verdict === "live" && row) {
            toast.success(`${row.name} connected.`);
            emitConnectorsChanged();
          } else if (verdict === "failed" && row)
            toast.error(row.last_error ?? "The connection didn't complete.");
          // "gone": removed while waiting - the disappearance speaks for itself.
        }
      });
    }, CONNECTOR_POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(every);
    };
  }, [watching, flows, dropFlow, onRows]);

  return { flows, connectExisting, connectFromCatalog, cancel: dropFlow };
}
