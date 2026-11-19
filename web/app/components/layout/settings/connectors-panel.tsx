/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { Plug, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router";
import { toast } from "@prismoid/blocks/toast";
import { EmptyState, LoadingState, PageHeader, Pill, Rows, Section } from "~/components/kit";
import {
  CONNECTOR_TONE,
  connectorStatusLabel,
  connectorVerbs,
  fetchConnectors,
  type ConnectFlow,
  type ConnectorRow,
} from "~/components/connectors/vocabulary";
import { useConnectFlows } from "~/components/connectors/use-connect-flows";
import { toastError, api, unwrap } from "~/lib/api/client";
import { closeSettings, emitConnectorsChanged } from "~/lib/settings-bus";

// ── Connectors: the quick roster; the full catalog + permissions live at /connectors ─────────
// (policies get ONE home - two full managers is how they drift. The panel keeps the
// heal verbs a user mid-dialog needs - Connect, Retry, Remove - and points at the page for
// everything else. The vocabulary and the OAuth dance moved to ~/components/connectors.)

export function ConnectorsPanel() {
  const [rows, setRows] = useState<ConnectorRow[] | null>(null);
  const [failed, setFailed] = useState(false);
  const load = useCallback(() => {
    setFailed(false);
    fetchConnectors()
      .then((page) => setRows(page.items))
      .catch(() => {
        setFailed(true);
        toast.error("Couldn't load connectors.");
      });
  }, []);
  useEffect(load, [load]);

  const flowsApi = useConnectFlows({ onRows: setRows });

  async function remove(row: ConnectorRow) {
    try {
      await unwrap(
        api.DELETE("/v1/connectors/{connector_id}", {
          params: { path: { connector_id: row.id } },
        }),
      );
      setRows((cur) => cur?.filter((c) => c.id !== row.id) ?? null);
      emitConnectorsChanged();
    } catch (e) {
      toastError(e, "Couldn't remove the connector.");
    }
  }

  // Retry = the probe, on demand - the recovery verb for rows OAuth cannot heal.
  const [probing, setProbing] = useState<string | null>(null);
  async function retryProbe(row: ConnectorRow) {
    if (probing !== null) return;
    setProbing(row.id);
    try {
      const fresh = await unwrap(
        api.POST("/v1/connectors/{connector_id}/probe", {
          params: { path: { connector_id: row.id } },
        }),
      );
      setRows((cur) => cur?.map((c) => (c.id === fresh.id ? fresh : c)) ?? null);
      if (fresh.status === "live") {
        toast.success(`${fresh.name} connected.`);
        emitConnectorsChanged();
      } else {
        toast.error(fresh.last_error ?? "The server still didn't answer.");
      }
    } catch (e) {
      toastError(e, "Couldn't reach the server.");
    } finally {
      setProbing(null);
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        as="h2"
        title="Connectors"
        description="External services your tasks can call. The catalog, per-tool permissions, and new connections live on the Connectors page."
      />

      <Section
        title="Connectors"
        actions={
          <Button
            variant="secondary"
            size="sm"
            stretch="auto"
            nativeButton={false}
            render={<Link to="/connectors" onClick={() => closeSettings()} />}
            icon={<Icon icon={Plug} />}
            label="Open connectors"
          />
        }
      >
        {rows === null ? (
          failed ? (
            <EmptyState
              compact
              icon={Plug}
              title="Couldn't load connectors"
              description="The list didn't arrive."
              action={
                <Button
                  variant="secondary"
                  size="sm"
                  stretch="auto"
                  onClick={load}
                  label="Try again"
                />
              }
            />
          ) : (
            <LoadingState />
          )
        ) : rows.length === 0 ? (
          <EmptyState
            compact
            icon={Plug}
            title="No connectors yet"
            description="Browse the catalog on the Connectors page, or add any MCP server by URL there."
          />
        ) : (
          <Rows as="ul">
            {rows.map((c) => (
              <ConnectorRowItem
                key={c.id}
                row={c}
                flow={flowsApi.flows[c.id] ?? null}
                probing={probing === c.id}
                onConnect={() => void flowsApi.connectExisting(c.id)}
                onCancel={() => flowsApi.cancel(c.id)}
                onRetry={() => void retryProbe(c)}
                onRemove={() => void remove(c)}
              />
            ))}
          </Rows>
        )}
      </Section>
    </div>
  );
}

/** One connector row - exported so its faces (status × flow) test as pure markup. */
export function ConnectorRowItem({
  row,
  flow,
  probing,
  onConnect,
  onCancel,
  onRetry,
  onRemove,
}: {
  row: ConnectorRow;
  flow: ConnectFlow | null;
  probing: boolean;
  onConnect: () => void;
  onCancel: () => void;
  onRetry: () => void;
  onRemove: () => void;
}) {
  const verbs = connectorVerbs(row);
  return (
    <li className="flex items-center gap-3 px-4 py-3 text-body-sm-regular">
      <Plug className="text-muted-foreground size-4 shrink-0" />
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium">
          {row.name}
          <span className="text-muted-foreground ml-2 text-caption-md-regular font-regular">
            {row.tools.length > 0 && `${row.tools.length} tool${row.tools.length === 1 ? "" : "s"}`}
          </span>
        </p>
        <p className="text-muted-foreground truncate text-caption-md-regular">
          {row.status === "error" && row.last_error ? row.last_error : row.server_url}
        </p>
      </div>
      <Pill tone={CONNECTOR_TONE[row.status]}>{connectorStatusLabel(row.status)}</Pill>
      {flow ? (
        <>
          {flow.phase === "waiting" && !flow.opened && flow.url && (
            // The popup was blocked - the same consent, as a link the user opens themself.
            <Button
              variant="secondary"
              size="sm"
              stretch="auto"
              nativeButton={false}
              render={<a href={flow.url} target="_blank" rel="noopener noreferrer" />}
              label="Open authorization page"
            />
          )}
          <span className="text-muted-foreground text-caption-md-regular" aria-live="polite">
            {flow.phase === "starting" ? "Starting…" : "Waiting for the provider…"}
          </span>
          {flow.phase === "waiting" && (
            <Button variant="ghost" size="sm" stretch="auto" onClick={onCancel} label="Cancel" />
          )}
        </>
      ) : verbs.connect ? (
        <Button variant="primary" size="sm" stretch="auto" onClick={onConnect} label="Connect" />
      ) : verbs.retry ? (
        <Button
          variant="secondary"
          size="sm"
          stretch="auto"
          onClick={onRetry}
          disabled={probing}
          label={probing ? "Probing…" : "Retry"}
        />
      ) : null}
      <IconButton
        variant="ghost"
        size="sm"
        aria-label={`Remove ${row.name}`}
        onClick={onRemove}
        icon={<Icon icon={Trash2} />}
      />
    </li>
  );
}
