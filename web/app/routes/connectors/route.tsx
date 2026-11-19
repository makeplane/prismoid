/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// /connectors (ADR-054) - the one home for services: the catalog as cards, your rows folded
// in, the peek with the per-tool permissions. The first settings-shaped route, built to the
// list-page checklist: no in-page title (the breadcrumb names it), standing controls on the
// topbar (search · category funnel · + Custom connector), the All/Connected/Available lens
// as SectionTab chips, state URL-borne where shareable (?filter, ?category, ?connector).
// The OAuth callback lands here too (?connected={slug}): close-or-toast, then the peek.

import {
  Dialog,
  DialogClose,
  DialogCloseGroup,
  DialogContent,
  DialogTitle,
} from "@makeplane/propel/components/dialog";
import {
  Menu,
  MenuContent,
  MenuGroup,
  MenuLabel,
  MenuRadioGroup,
  MenuRadioItem,
  MenuTrigger,
} from "@makeplane/propel/components/menu";
import { Plug, ListFilter, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useRevalidator, useSearchParams } from "react-router";
import { revalidateOnPageChange } from "~/lib/pending-route";
import { toast } from "@prismoid/blocks/toast";
import {
  fetchCatalog,
  filterCards,
  categoriesOf,
  mergeCatalog,
  type CardFilter,
} from "~/components/connectors/catalog";
import { useConnectFlows } from "~/components/connectors/use-connect-flows";
import { fetchConnectors, type ConnectorRow } from "~/components/connectors/vocabulary";
import { EmptyState, SearchBox, ToggleChip } from "~/components/kit";
import { SectionTab } from "~/components/layout/section-tab";
import { TopbarPortal } from "~/components/layout/topbar-slot";
import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { api, toastError, unwrap } from "~/lib/api/client";
import { onConnectorsChanged } from "~/lib/settings-bus";
import { useDebouncedValue } from "~/lib/paging";
import { AddConnector } from "./add-connector";
import { OfferFace } from "./offer-face";
import { ConnectorCardFace } from "./card";
import { ConnectorPeek } from "./peek";
import type { Route } from "./+types/route";

export function meta() {
  return [{ title: "Connectors · Prismoid" }];
}

export async function clientLoader() {
  // Roster first-class, catalog best-effort: the page must render your connectors even when
  // the catalog endpoint is unreachable (an older API, a blip) - cards just lose the offers.
  const [page, catalog] = await Promise.all([fetchConnectors(), fetchCatalog()]);
  return { rows: page.items, catalog };
}
clientLoader.hydrate = true as const;

// A tab, a filter or an open peek rides the query string: the page already holds its data, so
// the router does not hold that click for another fetch.
export const shouldRevalidate = revalidateOnPageChange;

const FILTERS: readonly { key: CardFilter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "connected", label: "Connected" },
  { key: "available", label: "Available" },
];

export default function Connectors({ loaderData }: Route.ComponentProps) {
  const revalidator = useRevalidator();
  const [rows, setRows] = useState<ConnectorRow[]>(loaderData.rows);
  useEffect(() => setRows(loaderData.rows), [loaderData.rows]);
  useEffect(() => onConnectorsChanged(() => void revalidator.revalidate()), [revalidator]);

  const [q, setQ] = useState("");
  const dq = useDebouncedValue(q.trim(), 250);
  const [searchParams, setSearchParams] = useSearchParams();
  const setParam = (key: string, value: string | null) =>
    setSearchParams(
      (p) => {
        const n = new URLSearchParams(p);
        if (value) n.set(key, value);
        else n.delete(key);
        return n;
      },
      { replace: true },
    );

  const rawFilter = searchParams.get("filter");
  const filter: CardFilter =
    rawFilter === "connected" || rawFilter === "available" ? rawFilter : "all";
  const category = searchParams.get("category");
  const categories = categoriesOf(loaderData.catalog);

  const flowsApi = useConnectFlows({ onRows: setRows });
  const fold = (fresh: ConnectorRow) =>
    setRows((cur) => cur.map((c) => (c.id === fresh.id ? fresh : c)));

  // ── the peek (?connector={id or slug}; slug serves catalog deep links pre-connect) ──────────
  const openRef = searchParams.get("connector");
  const openRow = openRef
    ? (rows.find((r) => r.id === openRef) ?? rows.find((r) => r.slug === openRef) ?? null)
    : null;
  // A catalog-only ref (an offered card, a docs deep link) opens the ENTRY face of the same
  // modal - the directory item is clickable whether or not it is connected yet.
  const openEntry =
    !openRow && openRef ? (loaderData.catalog?.find((e) => e.slug === openRef) ?? null) : null;

  // ── the OAuth landing (?connected={slug}) ───────────────────────────────────────────────────
  // The consent tab 302s here. A script-opened popup closes itself; a user-opened tab stays
  // and becomes the connector's page: toast, refresh, open the peek, strip the param.
  const connected = searchParams.get("connected");
  useEffect(() => {
    if (!connected) return;
    const ok = searchParams.get("ok") !== "0";
    window.close(); // inert unless this tab was script-opened - then the opener's poll speaks
    const timer = window.setTimeout(() => {
      if (ok) toast.success("Connected.");
      else toast.error("The connection didn't complete.");
      // The reload first, then the URL edit: a query-string edit no longer re-reads the roster,
      // and one that overtook the reload would cancel it.
      void revalidator.revalidate().then(() => landed());
    }, 250);
    const landed = () =>
      setSearchParams(
        (p) => {
          const n = new URLSearchParams(p);
          n.delete("connected");
          n.delete("ok");
          if (ok) n.set("connector", connected);
          return n;
        },
        { replace: true },
      );
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- searchParams deliberately not a dep: the effect keys on the landing param alone
  }, [connected]);

  async function remove(row: ConnectorRow) {
    try {
      await unwrap(
        api.DELETE("/v1/connectors/{connector_id}", {
          params: { path: { connector_id: row.id } },
        }),
      );
      setParam("connector", null);
      setRows((cur) => cur.filter((c) => c.id !== row.id));
    } catch (e) {
      toastError(e, "Couldn't remove the connector.");
    }
  }

  async function retryProbe(row: ConnectorRow) {
    try {
      const fresh = await unwrap(
        api.POST("/v1/connectors/{connector_id}/probe", {
          params: { path: { connector_id: row.id } },
        }),
      );
      fold(fresh);
      if (fresh.status === "live") toast.success(`${fresh.name} connected.`);
      else toast.error(fresh.last_error ?? "The server still didn't answer.");
    } catch (e) {
      toastError(e, "Couldn't reach the server.");
    }
  }

  const cards = useMemo(() => mergeCatalog(loaderData.catalog, rows), [loaderData.catalog, rows]);
  const shown = filterCards(cards, { filter, category, q: dq });

  return (
    <SectionTab
      controls={
        <>
          {FILTERS.map((f) => (
            <ToggleChip
              key={f.key}
              active={filter === f.key}
              onClick={() => setParam("filter", f.key === "all" ? null : f.key)}
            >
              {f.label}
            </ToggleChip>
          ))}
        </>
      }
    >
      <TopbarPortal>
        <SearchBox value={q} onChange={setQ} placeholder="Search connectors…" className="w-56" />
        {categories.length > 0 && (
          <Menu>
            <MenuTrigger
              render={
                category ? (
                  <Button
                    variant="tertiary"
                    size="sm"
                    stretch="auto"
                    icon={<Icon icon={ListFilter} />}
                    label={category}
                    aria-label="Filter by category"
                  />
                ) : (
                  <IconButton
                    variant="ghost"
                    size="sm"
                    icon={<Icon icon={ListFilter} />}
                    aria-label="Filter by category"
                  />
                )
              }
            />
            <MenuContent align="end">
              <MenuGroup>
                <MenuLabel>Category</MenuLabel>
                <MenuRadioGroup
                  value={category ?? "any"}
                  onValueChange={(v) => setParam("category", v === "any" ? null : v)}
                >
                  <MenuRadioItem value="any" label="Any" />
                  {categories.map((c) => (
                    <MenuRadioItem key={c} value={c} label={c} />
                  ))}
                </MenuRadioGroup>
              </MenuGroup>
            </MenuContent>
          </Menu>
        )}
        <AddConnector
          onAdded={(c) => {
            setRows((cur) => [c, ...cur]);
            setParam("connector", c.id);
          }}
        />
      </TopbarPortal>

      {shown.length === 0 ? (
        <EmptyState
          icon={Plug}
          title={dq ? `No matches for “${dq}”` : "No connectors here"}
          description={
            dq
              ? undefined
              : filter === "connected"
                ? "Connect a service from the catalog, or add a custom MCP server by URL."
                : "Add a custom MCP server by URL."
          }
        />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {shown.map((card) => (
            <ConnectorCardFace
              key={card.kind === "mine" ? card.row.id : card.entry.slug}
              card={card}
              flow={
                card.kind === "mine"
                  ? (flowsApi.flows[card.row.id] ?? null)
                  : (flowsApi.flows[card.entry.slug] ?? null)
              }
              onOpen={() =>
                setParam("connector", card.kind === "mine" ? card.row.id : card.entry.slug)
              }
              onConnect={(slug) => {
                void flowsApi.connectFromCatalog(slug).then((row) => {
                  if (row) setRows((cur) => [row, ...cur.filter((c) => c.id !== row.id)]);
                });
              }}
            />
          ))}
        </div>
      )}

      {/* The detail is a MODAL, centered ("a modal form", the drawer
          felt like a slider) - the settings dialog's scale, the peek's content, URL-borne
          ?connector= exactly as before. */}
      <Dialog
        open={openRow !== null || openEntry !== null}
        onOpenChange={(o) => !o && setParam("connector", null)}
      >
        {/* md is 672px, the old 42rem; the faces own their padding and scrolling. */}
        <DialogContent size="md">
          {!openRow && openEntry && (
            <>
              <div className="sr-only">
                <DialogTitle>{openEntry.name}</DialogTitle>
              </div>
              <OfferFace
                entry={openEntry}
                flow={flowsApi.flows[openEntry.slug] ?? null}
                onConnect={(slug) => {
                  void flowsApi.connectFromCatalog(slug).then((row) => {
                    if (row) {
                      setRows((cur) => [row, ...cur.filter((c) => c.id !== row.id)]);
                      setParam("connector", row.id); // the modal follows the row it just made
                    }
                  });
                }}
              />
            </>
          )}
          {openRow && (
            <>
              <div className="sr-only">
                <DialogTitle>{openRow.name}</DialogTitle>
              </div>
              <ConnectorPeek
                row={openRow}
                entry={
                  (openRow.catalog_slug &&
                    loaderData.catalog?.find((e) => e.slug === openRow.catalog_slug)) ||
                  null
                }
                flow={flowsApi.flows[openRow.id] ?? null}
                onConnect={() => void flowsApi.connectExisting(openRow.id)}
                onCancelFlow={() => flowsApi.cancel(openRow.id)}
                onRetry={() => void retryProbe(openRow)}
                onRemove={() => void remove(openRow)}
                onFresh={fold}
              />
            </>
          )}
          <DialogCloseGroup>
            <IconButton
              variant="ghost"
              size="xs"
              aria-label="Close"
              icon={<Icon icon={X} />}
              render={<DialogClose />}
            />
          </DialogCloseGroup>
        </DialogContent>
      </Dialog>
    </SectionTab>
  );
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
