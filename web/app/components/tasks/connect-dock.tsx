/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The connect block (ADR-054) - the conversation's own "Connect Notion" card. A step asked
// for a service the task does not hold; instead of a dead-end sentence, this dock renders the
// service's face (brand mark, the agent's one-sentence why), runs the SAME one-click OAuth
// dance the connectors page uses, and - the whole point - RESOLVES ITSELF the moment the
// connector goes live: accept attaches it server-side and the run continues into progress
// with the tools mounted. Skip declines (the task continues without, honestly); the TTL
// declines on its own.
//
// ITS OWN SPECIES, deliberately (by design: "this needs to be a different
// component... just a connect button; yes/no options and stuff shouldn't be there"). The
// question and approval docks speak the Q&A grammar - title, option rows, verb pairs. This
// block speaks the DIRECTORY's: a CONNECTION NEEDED kicker, the size-12 brand well nothing
// in Q&A land wears, the service's name as the headline, and exactly ONE primary action -
// Connect. Continuing without is a quiet text link, never a chip that mirrors Skip.

import { ArrowRight, Check, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { ConnectorIcon } from "~/components/connectors/connector-icon";
import { fetchCatalog, type CatalogEntry } from "~/components/connectors/catalog";
import { useConnectFlows } from "~/components/connectors/use-connect-flows";
import { fetchConnectors, type ConnectorRow } from "~/components/connectors/vocabulary";
import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { api, toastError, unwrap } from "~/lib/api/client";
import { onConnectorsChanged } from "~/lib/settings-bus";
import type { GateView } from "~/lib/task-view";
import { cn } from "~/lib/utils";

/** The service the block offers, from the gate + what the client can see: the catalog entry
 *  (face + auth note) and the user's own row when one exists. Pure - tested apart. */
export function connectOffer(
  gate: Pick<GateView, "connector_slug" | "title">,
  catalog: CatalogEntry[] | null,
  roster: ConnectorRow[],
): {
  slug: string;
  display: string;
  entry: CatalogEntry | null;
  row: ConnectorRow | null;
  live: boolean;
} {
  const slug = gate.connector_slug ?? "";
  const entry = (slug && catalog?.find((e) => e.slug === slug)) || null;
  const row = (slug && roster.find((r) => r.slug === slug || r.catalog_slug === slug)) || null;
  const display =
    row?.name ??
    entry?.name ??
    gate.title?.replace(/^Connect /, "").replace(/ to continue\?$/, "") ??
    "the service";
  return { slug, display, entry, row, live: row?.status === "live" };
}

/** The block itself, shared by the task page's dock and the inbox's gate card - one face,
 *  wherever the gate is met. Owns its data (catalog + roster, bus-refreshed) and its resolve
 *  calls; `footer` lets the inbox card append its Cancel-task verb. */
export function ConnectGateBody({
  gate,
  onResolved,
  footer,
}: {
  gate: GateView;
  onResolved?: () => void;
  footer?: React.ReactNode;
}) {
  const [catalog, setCatalog] = useState<CatalogEntry[] | null>(null);
  const [roster, setRoster] = useState<ConnectorRow[]>([]);
  const [busy, setBusy] = useState(false);
  const resolvedOnce = useRef(false);

  const load = () => {
    void fetchCatalog().then((c) => setCatalog(c));
    void fetchConnectors()
      .then((page) => setRoster(page.items))
      .catch(() => {});
  };
  useEffect(load, []);
  useEffect(() => onConnectorsChanged(load), []);

  const flowsApi = useConnectFlows({ onRows: setRoster });
  const offer = connectOffer(gate, catalog, roster);
  const flow =
    (offer.row && flowsApi.flows[offer.row.id]) ||
    (offer.slug && flowsApi.flows[offer.slug]) ||
    null;

  async function resolve(action: "accept" | "decline") {
    if (busy || resolvedOnce.current) return;
    setBusy(true);
    resolvedOnce.current = true;
    try {
      await unwrap(
        api.POST("/v1/gates/{gate_id}/resolve", {
          params: { path: { gate_id: gate.gate_id } },
          body: { action },
        }),
      );
      onResolved?.();
    } catch (e) {
      resolvedOnce.current = false;
      toastError(e, action === "accept" ? "Couldn't continue." : "Couldn't skip.");
    } finally {
      setBusy(false);
    }
  }

  // A user gesture on THIS card started a connect (or a live-attach). Only then does going
  // live auto-accept - a pre-existing live row the owner never clicked Connect for on this
  // task (auto_attach=false is theirs to set), or a co-editor's own matching row, must not be
  // attached by a mere page view. `didConnect` arms on the Connect click below.
  const didConnect = useRef(false);

  // THE point of the block: once the user has asked to connect, the moment the connector is
  // live, accept fires by itself and the task continues into progress. Once, guarded - and a
  // failed accept re-arms so the manual Connect button can retry it.
  useEffect(() => {
    if (offer.live && didConnect.current && !resolvedOnce.current) void resolve("accept");
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed on the connector going live; resolve is a fresh closure each render and the once-guard makes it safe
  }, [offer.live, flow]);

  function connect() {
    didConnect.current = true; // arm the auto-accept: this task's user asked to connect
    if (offer.row) void flowsApi.connectExisting(offer.row.id);
    else if (offer.slug) void flowsApi.connectFromCatalog(offer.slug);
  }

  // The quiet subordinate action: a text link, never a chip. Available on the live face too,
  // so a card that reached live without an auto-accept (a pre-existing row, or a failed one)
  // is never a dead end - the user can still decline out of it.
  const continueWithout = (
    <button
      type="button"
      disabled={busy}
      title="Continue without connecting - the task will say what was skipped"
      className="text-muted-foreground hover:text-foreground text-caption-md-regular underline-offset-2 transition-colors hover:underline disabled:opacity-50"
      onClick={() => void resolve("decline")}
    >
      Continue without it
    </button>
  );

  return (
    <div className="flex items-center gap-4">
      {/* the directory's face, not the questionnaire's: the size-12 brand well leads */}
      <ConnectorIcon slug={offer.slug || "connector"} icon={offer.entry?.icon} size="lg" />
      <div className="min-w-0 flex-1">
        <p className="text-muted-foreground text-caption-md-regular font-medium tracking-wide uppercase">
          {offer.live ? "Connected" : "Connection needed"}
        </p>
        <h3 className="mt-0.5 text-body-sm-regular font-medium break-words">{offer.display}</h3>
        {gate.reason && (
          <p className="text-muted-foreground mt-0.5 text-caption-md-regular break-words">
            {gate.reason}
          </p>
        )}
        {!offer.live && !flow && offer.entry?.auth_note && (
          <p className="text-muted-foreground/80 mt-1 text-caption-md-regular break-words">
            {offer.entry.auth_note}
          </p>
        )}
        {(continueWithout || footer) && (
          <div className="mt-1.5 flex items-center gap-3">
            {continueWithout}
            {footer}
          </div>
        )}
      </div>
      <div className="flex shrink-0 flex-col items-end gap-1.5">
        {offer.live ? (
          busy || resolvedOnce.current ? (
            <span
              className="text-muted-foreground flex items-center gap-1.5 text-caption-md-regular"
              aria-live="polite"
            >
              <Check className="size-3.5" /> Continuing…
            </span>
          ) : (
            // Live but not yet accepted (a pre-existing row, or a failed auto-accept): the
            // attach needs an explicit click - a page view must not mount a credential.
            <Button
              variant="primary"
              size="md"
              stretch="auto"
              icon={<Icon icon={ArrowRight} />}
              iconPosition="end"
              label={`Continue with ${offer.display}`}
              disabled={busy}
              onClick={() => void resolve("accept")}
            />
          )
        ) : flow ? (
          <>
            <span className="text-muted-foreground text-caption-md-regular" aria-live="polite">
              {flow.phase === "starting" ? "Starting…" : "Waiting for the provider…"}
            </span>
            {flow.phase === "waiting" && !flow.opened && flow.url && (
              <Button
                nativeButton={false}
                render={<a href={flow.url} target="_blank" rel="noopener noreferrer" />}
                variant="secondary"
                size="sm"
                stretch="auto"
                label="Open authorization page"
              />
            )}
            {flow.phase === "waiting" && offer.row && (
              <button
                type="button"
                className="text-muted-foreground hover:text-foreground text-caption-md-regular underline-offset-2 transition-colors hover:underline"
                onClick={() => flowsApi.cancel(offer.row!.id)}
              >
                Cancel
              </button>
            )}
          </>
        ) : offer.slug ? (
          <Button
            variant="primary"
            size="md"
            stretch="auto"
            icon={<Icon icon={ArrowRight} />}
            iconPosition="end"
            label={`Connect ${offer.display}`}
            disabled={busy}
            onClick={connect}
          />
        ) : (
          // Nothing matched the agent's word: the block stays honest and points at the page.
          <Button
            nativeButton={false}
            render={<Link to="/connectors" />}
            variant="secondary"
            size="md"
            stretch="auto"
            label="Open connectors"
          />
        )}
      </div>
    </div>
  );
}

/** The task page's docked face. Floats where the docks float, but wears its OWN shell:
 *  the p-4 connection card, never the Q&A docks' p-2 questionnaire chrome. */
export function ConnectDock({
  gate,
  onResolved,
  className,
}: {
  gate: GateView;
  onResolved?: () => void;
  className?: string;
}) {
  const [collapsed, setCollapsed] = useState(false);

  if (collapsed) {
    return (
      <button
        type="button"
        onClick={() => setCollapsed(false)}
        className={cn(
          "bg-popover ring-foreground/10 shadow-(--shadow-overlay) hover:bg-muted/40 flex w-full items-center justify-between rounded-xl px-4 py-2.5 text-left ring-1 transition-colors",
          className,
        )}
      >
        <span className="text-body-sm-regular font-medium">A connection is waiting</span>
        <span className="text-muted-foreground text-caption-md-regular">Review</span>
      </button>
    );
  }

  return (
    <div
      className={cn(
        "bg-popover ring-foreground/10 relative rounded-xl p-4 shadow-(--shadow-overlay) ring-1",
        className,
      )}
    >
      <span className="absolute top-2 right-2 flex">
        <IconButton
          variant="ghost"
          size="sm"
          icon={<Icon icon={X} />}
          aria-label="Hide the connection request"
          onClick={() => setCollapsed(true)}
        />
      </span>
      <ConnectGateBody gate={gate} onResolved={onResolved} />
    </div>
  );
}
