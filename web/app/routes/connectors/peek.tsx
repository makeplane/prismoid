/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The connector detail peek (ADR-054) - overview, verbs, and THE permissions screen:
// per-tool Off · Ask · Allow rows grouped read-only vs write, group bulk-set, the
// auto-attach dial. Capability law throughout: tool sections render only once a probe
// cached an inventory; Connect never shows on live; nothing is disabled-with-an-excuse.

import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { ExternalLink, Plug, Trash2 } from "lucide-react";
import { useState } from "react";
import { ConnectorIcon } from "~/components/connectors/connector-icon";
import {
  disableConnector,
  enableConnector,
  saveToolPolicies,
  setAutoAttach,
  type CatalogEntry,
  type ToolPolicy,
} from "~/components/connectors/catalog";
import {
  POLICY_META,
  POLICY_ORDER,
  groupSummary,
  policyGroups,
  withGroupPolicy,
  withPolicy,
  type PolicyRow,
} from "~/components/connectors/policies";
import {
  CONNECTOR_TONE,
  type ConnectFlow,
  type ConnectorRow,
  connectorStatusLabel,
  connectorVerbs,
} from "~/components/connectors/vocabulary";
import { Pill, Rows, Section, ToggleChip } from "~/components/kit";
import { Switch } from "@makeplane/propel/components/switch";
import { toastError } from "~/lib/api/client";
import { emitConnectorsChanged } from "~/lib/settings-bus";

export function ConnectorPeek({
  row,
  entry,
  flow,
  onConnect,
  onCancelFlow,
  onRetry,
  onRemove,
  onFresh,
}: {
  row: ConnectorRow;
  entry: CatalogEntry | null;
  flow: ConnectFlow | null;
  onConnect: () => void;
  onCancelFlow: () => void;
  onRetry: () => void;
  onRemove: () => void;
  /** A mutation answered with the fresh row - the route folds it into its roster. */
  onFresh: (row: ConnectorRow) => void;
}) {
  const verbs = connectorVerbs(row);
  const [saving, setSaving] = useState(false);

  async function mutate(run: () => Promise<ConnectorRow>, failure: string) {
    if (saving) return;
    setSaving(true);
    try {
      onFresh(await run());
      emitConnectorsChanged();
    } catch (e) {
      toastError(e, failure);
    } finally {
      setSaving(false);
    }
  }

  const setPolicies = (policies: Record<string, ToolPolicy>) =>
    mutate(() => saveToolPolicies(row.id, policies), "Couldn't save the permissions.");

  const groups = policyGroups(row.tools);
  const showTools = row.tools.length > 0 && row.status !== "disabled";

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      <div className="space-y-6 p-6">
        <div className="flex items-start gap-4">
          <ConnectorIcon slug={row.catalog_slug ?? row.slug} icon={entry?.icon} size="lg" />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <h2 className="font-heading truncate text-h4-regular font-semibold tracking-tight">
                {row.name}
              </h2>
              <Pill tone={CONNECTOR_TONE[row.status]}>{connectorStatusLabel(row.status)}</Pill>
            </div>
            <p className="text-muted-foreground mt-0.5 truncate font-mono text-caption-md-regular">
              {row.server_url}
            </p>
            {(entry?.description || row.last_error) && (
              <p className="text-muted-foreground mt-2 text-body-sm-regular">
                {row.status === "error" || row.status === "needs_reauth"
                  ? (row.last_error ?? entry?.description)
                  : entry?.description}
              </p>
            )}
            {entry?.auth_note && row.status !== "live" && (
              // The catch, said up front: e.g. Google ships no dynamic client
              // registration - the card must not promise a one-click it cannot keep.
              <p className="text-muted-foreground mt-2 text-caption-md-regular">
                {entry.auth_note}
              </p>
            )}
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {flow ? (
            <>
              {flow.phase === "waiting" && !flow.opened && flow.url && (
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
                <Button
                  variant="ghost"
                  size="sm"
                  stretch="auto"
                  onClick={onCancelFlow}
                  label="Cancel"
                />
              )}
            </>
          ) : (
            <>
              {verbs.connect && (
                <Button
                  variant="primary"
                  size="sm"
                  stretch="auto"
                  onClick={onConnect}
                  icon={<Icon icon={Plug} />}
                  label={row.status === "needs_reauth" ? "Reconnect" : "Connect"}
                />
              )}
              {verbs.retry && (
                <Button
                  variant="secondary"
                  size="sm"
                  stretch="auto"
                  onClick={onRetry}
                  label="Retry"
                />
              )}
              {verbs.disable && (
                <Button
                  variant="ghost"
                  size="sm"
                  stretch="auto"
                  onClick={() =>
                    void mutate(() => disableConnector(row.id), "Couldn't disable it.")
                  }
                  label="Disable"
                />
              )}
              {verbs.enable && (
                <Button
                  variant="primary"
                  size="sm"
                  stretch="auto"
                  onClick={() => void mutate(() => enableConnector(row.id), "Couldn't enable it.")}
                  label="Enable"
                />
              )}
            </>
          )}
          <span className="flex-1" />
          {entry?.docs_url && (
            <Button
              variant="ghost"
              size="sm"
              stretch="auto"
              nativeButton={false}
              render={<a href={entry.docs_url} target="_blank" rel="noopener noreferrer" />}
              icon={<Icon icon={ExternalLink} />}
              label="Docs"
            />
          )}
          <IconButton
            variant="ghost"
            size="sm"
            aria-label={`Remove ${row.name}`}
            onClick={onRemove}
            icon={<Icon icon={Trash2} />}
          />
        </div>

        {row.status === "live" && (
          <label className="flex items-center justify-between gap-3 text-body-sm-regular">
            <span>
              <span className="font-medium">Offer automatically</span>
              <span className="text-muted-foreground block text-caption-md-regular">
                A task whose wording matches this service attaches it on its own; tools set to Ask
                still pause for you.
              </span>
            </span>
            <Switch
              size="md"
              checked={row.auto_attach}
              onCheckedChange={(on) =>
                void mutate(() => setAutoAttach(row.id, on === true), "Couldn't save the dial.")
              }
              aria-label="Attach this connector automatically when a task's wording matches"
            />
          </label>
        )}

        {showTools && (
          <>
            <PolicyGroup
              title="Read-only tools"
              rows={groups.readOnly}
              busy={saving}
              onRow={(name, value) => void setPolicies(withPolicy(row.tools, name, value))}
              onAll={(value) =>
                void setPolicies(
                  withGroupPolicy(
                    row.tools,
                    groups.readOnly.map((r) => r.name),
                    value,
                  ),
                )
              }
            />
            <PolicyGroup
              title="Write tools"
              rows={groups.write}
              busy={saving}
              onRow={(name, value) => void setPolicies(withPolicy(row.tools, name, value))}
              onAll={(value) =>
                void setPolicies(
                  withGroupPolicy(
                    row.tools,
                    groups.write.map((r) => r.name),
                    value,
                  ),
                )
              }
            />
          </>
        )}
      </div>
    </div>
  );
}

/** One group of per-tool tri-states. The Section header carries the bulk trio; "mixed"
 *  shows as none-active - three visible states beat a dropdown hiding two of them. */
function PolicyGroup({
  title,
  rows,
  busy,
  onRow,
  onAll,
}: {
  title: string;
  rows: PolicyRow[];
  busy: boolean;
  onRow: (name: string, value: ToolPolicy) => void;
  onAll: (value: ToolPolicy) => void;
}) {
  if (rows.length === 0) return null;
  const summary = groupSummary(rows);
  return (
    <Section
      title={title}
      actions={
        <PolicyTrio value={summary === "mixed" ? null : summary} busy={busy} onPick={onAll} />
      }
    >
      <Rows>
        {rows.map((r) => (
          <div key={r.name} className="flex items-center gap-3 px-4 py-3">
            <div className="min-w-0 flex-1">
              <p className="truncate font-mono text-body-sm-regular">
                {r.name}
                {r.destructive && (
                  <span className="text-muted-foreground font-sans ml-2 text-caption-md-regular">
                    destructive
                  </span>
                )}
              </p>
              {r.description && (
                <p className="text-muted-foreground truncate text-caption-md-regular">
                  {r.description}
                </p>
              )}
            </div>
            <PolicyTrio value={r.policy} busy={busy} onPick={(v) => onRow(r.name, v)} />
          </div>
        ))}
      </Rows>
    </Section>
  );
}

function PolicyTrio({
  value,
  busy,
  onPick,
}: {
  value: ToolPolicy | null;
  busy: boolean;
  onPick: (value: ToolPolicy) => void;
}) {
  return (
    <span className="flex shrink-0 items-center gap-1" role="group" aria-label="Tool permission">
      {POLICY_ORDER.map((p) => (
        <ToggleChip
          key={p}
          active={value === p}
          disabled={busy}
          title={POLICY_META[p].blurb}
          onClick={() => value !== p && onPick(p)}
        >
          {POLICY_META[p].label}
        </ToggleChip>
      ))}
    </span>
  );
}
