/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The console's front page: is the machine healthy, is anything shouting, what did today
// cost. Health + alerts re-poll every 30s (visible-tab only) - an operator leaves this open.

import { Activity, BellOff, TriangleAlert } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useLoaderData } from "react-router";
import { EmptyState, MetaChip, Money, Pill, Rows, Section, type Tone } from "~/components/kit";
import { Badge } from "@makeplane/propel/components/badge";
import { api, unwrap } from "~/lib/api/client";
import type { components } from "~/lib/api/types.gen";

type HealthOut = components["schemas"]["AdminHealthOut"];
type AlertOut = components["schemas"]["AlertOut"];
type SummaryOut = components["schemas"]["SpendSummaryOut"];

export function meta() {
  return [{ title: "Admin · Prismoid" }];
}

export async function clientLoader() {
  const [health, summary] = await Promise.all([
    // ONE request for the card AND the list: `/admin/health` carries its own alerts, computed
    // from one reading (admin_health.Vitals). Fetched apart, this page paid for the stuck scan and
    // the catalog doctor twice - every poll, for every operator with the tab open.
    unwrap(api.GET("/v1/admin/health")),
    // Best-effort: a ledger hiccup must not blank the health view.
    unwrap(api.GET("/v1/admin/spend/summary", { params: { query: { days: 30 } } })).catch(
      () => null,
    ),
  ]);
  return { health, summary };
}
clientLoader.hydrate = true as const;

const POLL_MS = 30_000;

const SEVERITY_TONE: Record<string, Tone> = {
  warn: "warning",
  error: "danger",
};

export default function AdminOverview() {
  const loaderData = useLoaderData<typeof clientLoader>();
  const [health, setHealth] = useState<HealthOut>(loaderData.health);
  const summary: SummaryOut | null = loaderData.summary;
  const alerts: AlertOut[] = health.alerts;

  useEffect(() => {
    const id = window.setInterval(() => {
      if (document.hidden) return;
      void unwrap(api.GET("/v1/admin/health")).then(setHealth, () => {});
    }, POLL_MS);
    return () => window.clearInterval(id);
  }, []);

  const workerOk = health.worker.status === "ok";

  return (
    <div className="space-y-6">
      <Section title="Health">
        <div className="flex flex-wrap items-center gap-2">
          <Pill icon={Activity} tone={workerOk ? "success" : "danger"}>
            worker {health.worker.status}
            {health.worker.beat_age_s !== null && (
              <span className="tabular-nums">· beat {Math.round(health.worker.beat_age_s)}s</span>
            )}
          </Pill>
          {health.queues.map((q) => (
            <Pill key={q.name} tone={q.clogged ? "danger" : "muted"}>
              {q.name}
              <span className="tabular-nums">
                · {q.enqueued} queued · {q.running} running
              </span>
              {q.oldest_wait_s > 0 && (
                <span className="tabular-nums">· oldest {Math.round(q.oldest_wait_s)}s</span>
              )}
            </Pill>
          ))}
          <MetaChip>migration {health.migration}</MetaChip>
          {health.version_ghosts > 0 && (
            <MetaChip>
              {health.version_ghosts} version ghost
              {health.version_ghosts === 1 ? "" : "s"}
            </MetaChip>
          )}
        </div>
        {health.stuck_tasks > 0 && (
          <p className="text-body-sm-regular">
            <TriangleAlert className="text-warning mr-1.5 inline size-4 align-[-2px]" />
            {health.stuck_tasks} stuck task
            {health.stuck_tasks === 1 ? "" : "s"} ·{" "}
            <Link to="/tasks?stuck=true" className="underline underline-offset-2">
              open the console
            </Link>
          </p>
        )}
      </Section>

      <Section title="Alerts">
        {alerts.length === 0 ? (
          <EmptyState
            compact
            icon={BellOff}
            title="All quiet"
            description="Nothing is asking for an operator right now."
          />
        ) : (
          <Rows as="ul">
            {alerts.map((a) => (
              <li key={a.code} className="flex items-center gap-3 px-4 py-3">
                <Pill tone={SEVERITY_TONE[a.severity] ?? "muted"}>{a.severity}</Pill>
                <span className="min-w-0 flex-1">
                  <span className="flex items-center gap-2">
                    <Badge variant="neutral" size="xs" label={a.code} />
                    {a.count > 1 && (
                      <span className="text-muted-foreground text-caption-md-regular tabular-nums">
                        ×{a.count}
                      </span>
                    )}
                  </span>
                  <span className="text-muted-foreground mt-0.5 block text-caption-md-regular">
                    {a.detail}
                  </span>
                </span>
              </li>
            ))}
          </Rows>
        )}
      </Section>

      {summary && (
        <Section
          title="Spend"
          actions={
            <Link
              to="/spend"
              className="text-muted-foreground text-caption-md-regular underline underline-offset-2"
            >
              Full breakdown
            </Link>
          }
        >
          <p className="text-muted-foreground text-body-sm-regular">
            <Money usd={summary.today_usd} className="text-foreground" /> today ·{" "}
            <Money usd={summary.mtd_usd} className="text-foreground" /> this month ·{" "}
            <Money usd={summary.total_usd} className="text-foreground" /> over {summary.days} days ·{" "}
            <span className="tabular-nums">{summary.calls}</span> calls
          </p>
        </Section>
      )}
    </div>
  );
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
