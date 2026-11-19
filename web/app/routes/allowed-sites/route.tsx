/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// /allowed-sites (ADR-046 the web door) - the browser's standing egress grants: hosts a
// task's Always-allow gate minted, or an owner added by hand, so the per-open ask never
// fires for them again. The smallest list page: no filters, no paging - list, add, revoke.

import { Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { EmptyState, FormError, Pill, Rows } from "~/components/kit";
import { SectionTab } from "~/components/layout/section-tab";
import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { Input, InputGroup } from "@makeplane/propel/components/input";
import { api, ApiError, toastError, unwrap } from "~/lib/api/client";
import { formatDateTime } from "~/lib/format";
import type { components } from "~/lib/api/types.gen";
import type { Route } from "./+types/route";

export type EgressAllowanceRow = components["schemas"]["EgressAllowanceOut"];

export function meta() {
  return [{ title: "Allowed sites · Prismoid" }];
}

export async function clientLoader() {
  const rows = await unwrap(api.GET("/v1/egress-allowances"));
  return { rows };
}
clientLoader.hydrate = true as const;

/** A row's provenance, in the owner's words - never the wire's `gate` | `settings`. */
function sourceLabel(source: string): string {
  return source === "gate" ? "from a task" : "added here";
}

/** Pure list face - a `<ul>` of hosts (provenance badge, created date, a Revoke button), or
 *  the empty-state paragraph. Exported for its own render test; the route below supplies the
 *  data and the verbs. */
export function AllowedSitesList({
  rows,
  onRevoke,
}: {
  rows: EgressAllowanceRow[];
  onRevoke: (row: EgressAllowanceRow) => void;
}) {
  if (rows.length === 0) {
    return (
      <EmptyState
        title="No always-allowed sites."
        description="The browser asks before opening a new site; choose Always to add one here."
      />
    );
  }
  return (
    <Rows as="ul">
      {rows.map((row) => (
        <li key={row.id} className="flex items-center gap-3 px-4 py-3 text-body-sm-regular">
          <span className="min-w-0 flex-1">
            <span className="flex items-center gap-2">
              <span className="truncate font-medium">{row.host}</span>
              <Pill tone={row.source === "gate" ? "info" : "muted"}>{sourceLabel(row.source)}</Pill>
            </span>
            <span className="text-muted-foreground block text-caption-md-regular">
              {formatDateTime(row.created_at)}
            </span>
          </span>
          <Button
            variant="ghost"
            size="sm"
            stretch="auto"
            icon={<Icon icon={Trash2} />}
            label="Revoke"
            onClick={() => onRevoke(row)}
          />
        </li>
      ))}
    </Rows>
  );
}

export default function AllowedSites({ loaderData }: Route.ComponentProps) {
  const [rows, setRows] = useState<EgressAllowanceRow[]>(loaderData.rows);
  useEffect(() => setRows(loaderData.rows), [loaderData.rows]);

  const [host, setHost] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function add() {
    const trimmed = host.trim();
    if (!trimmed) return;
    setBusy(true);
    setFieldError(null);
    try {
      const row = await unwrap(api.POST("/v1/egress-allowances", { body: { host: trimmed } }));
      setRows((cur) => [row, ...cur]);
      setHost("");
    } catch (e) {
      if (e instanceof ApiError && e.status === 422 && e.fields?.host) {
        setFieldError(e.fields.host);
      } else {
        toastError(e, "Couldn't add that site.");
      }
    } finally {
      setBusy(false);
    }
  }

  async function revoke(row: EgressAllowanceRow) {
    try {
      await unwrap(
        api.DELETE("/v1/egress-allowances/{allowance_id}", {
          params: { path: { allowance_id: row.id } },
        }),
      );
      setRows((cur) => cur.filter((r) => r.id !== row.id));
    } catch (e) {
      toastError(e, "Couldn't revoke that site.");
    }
  }

  return (
    <SectionTab>
      <form
        className="flex items-start gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          void add();
        }}
      >
        <div className="min-w-0 flex-1 space-y-1.5">
          <InputGroup size="md">
            <Input
              size="md"
              aria-label="Host"
              value={host}
              onChange={(e) => {
                setHost(e.currentTarget.value);
                if (fieldError) setFieldError(null);
              }}
              placeholder="docs.vendor.com"
            />
          </InputGroup>
          <FormError>{fieldError}</FormError>
        </div>
        <Button
          type="submit"
          variant="primary"
          size="md"
          stretch="auto"
          label="Always allow"
          disabled={busy || !host.trim()}
        />
      </form>

      <AllowedSitesList rows={rows} onRevoke={(row) => void revoke(row)} />
    </SectionTab>
  );
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
