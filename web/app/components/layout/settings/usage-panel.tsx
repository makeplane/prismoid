/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Gauge } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "@prismoid/blocks/toast";
import { EmptyState, LoadingState, PageHeader, Rows, Section } from "~/components/kit";
import { api, unwrap } from "~/lib/api/client";

// ── Usage: month-to-date spend by project, read from the rollup (never the ledger) ──────────────

type ProjectUsageRow = {
  id: string;
  name: string;
  month_to_date_usd: string;
  budget_usd: string | null;
};

const fetchProjectUsage = async (): Promise<ProjectUsageRow[]> => {
  const page = await unwrap(api.GET("/v1/projects", { params: { query: { limit: 50 } } }));
  return Promise.all(
    page.items.map(async (s) => {
      // The usage route serializes Decimals as strings inside an untyped payload (dict[str, Any]).
      const u = (await unwrap(
        api.GET("/v1/projects/{project_id}/usage", {
          params: { path: { project_id: s.id } },
        }),
      )) as { month_to_date_usd?: string; budget_usd?: string | null };
      return {
        id: s.id,
        name: s.name,
        month_to_date_usd: u.month_to_date_usd ?? "0",
        budget_usd: u.budget_usd ?? null,
      };
    }),
  );
};

const usd = (v: string) => `$${Number(v).toFixed(2)}`;

export function UsagePanel() {
  const [rows, setRows] = useState<ProjectUsageRow[] | null>(null);
  useEffect(() => {
    fetchProjectUsage()
      .then(setRows)
      .catch(() => toast.error("Couldn't load usage."));
  }, []);

  const total = rows?.reduce((sum, r) => sum + Number(r.month_to_date_usd), 0);

  return (
    <div className="space-y-6">
      <PageHeader
        as="h2"
        title="Usage"
        description="Spend this calendar month, by project, from the usage rollup. Tasks outside a project aren't counted here."
      />
      {rows === null ? (
        <LoadingState />
      ) : rows.length === 0 ? (
        <EmptyState
          icon={Gauge}
          title="No projects yet"
          description="Spend shows up here once your tasks run inside projects."
        />
      ) : (
        <Section
          title="This month"
          actions={
            <span className="text-muted-foreground text-body-sm-regular tabular-nums">
              {usd(String(total ?? 0))} total
            </span>
          }
        >
          <Rows as="ul">
            {rows.map((r) => (
              <li key={r.id} className="flex items-center gap-3 px-4 py-3 text-body-sm-regular">
                <span className="min-w-0 flex-1 truncate font-medium">{r.name}</span>
                <span className="text-muted-foreground shrink-0 text-caption-md-regular tabular-nums">
                  {r.budget_usd !== null && `of ${usd(r.budget_usd)} budget`}
                </span>
                <span className="shrink-0 tabular-nums">{usd(r.month_to_date_usd)}</span>
              </li>
            ))}
          </Rows>
        </Section>
      )}
    </div>
  );
}
