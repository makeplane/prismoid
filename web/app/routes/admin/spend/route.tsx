/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The ledger, at operator altitude: what the window cost, what today cost, who and what is
// spending it. The chart is inline SVG off the pure series helpers (lib/spend-series) - no
// chart lib; grouped series wear the categorical hue ramp (kit tone.ts - user data, never
// status), the ungrouped total wears primary. Zero-cost calls get a warning tile only when
// they exist - a healthy ledger doesn't show a zero warning.

import { useEffect, useState } from "react";
import { Link, useLoaderData, useSearchParams } from "react-router";
import { revalidateOnPageChange } from "~/lib/pending-route";
import {
  EmptyState,
  HUE_TEXT,
  HUE_TINT,
  LoadingState,
  MetaChip,
  Money,
  Pill,
  Rows,
  Section,
  ToggleChip,
} from "~/components/kit";
import { Card, CardContent } from "@prismoid/blocks/card";
import { api, unwrap } from "~/lib/api/client";
import { buildSpendSeries, dailyTotals, linePoints, type SpendPoint } from "~/lib/spend-series";
import { useUrlParam } from "~/lib/url-state";
import { cn } from "~/lib/utils";
import { Coins } from "lucide-react";

export function meta() {
  return [{ title: "Spend · Admin · Prismoid" }];
}

const DAYS = 30;

export async function clientLoader() {
  const [summary, daily] = await Promise.all([
    unwrap(api.GET("/v1/admin/spend/summary", { params: { query: { days: DAYS } } })),
    unwrap(api.GET("/v1/admin/spend/daily", { params: { query: { days: DAYS } } })),
  ]);
  return { summary, daily };
}
clientLoader.hydrate = true as const;

// A tab, a filter or an open peek rides the query string: the page already holds its data, so
// the router does not hold that click for another fetch.
export const shouldRevalidate = revalidateOnPageChange;

type Group = "total" | "model" | "user";
const GROUPS: { key: Group; label: string }[] = [
  { key: "total", label: "Total" },
  { key: "model", label: "By model" },
  { key: "user", label: "By user" },
];

export default function AdminSpend() {
  const loaderData = useLoaderData<typeof clientLoader>();
  const { summary } = loaderData;
  // URL-borne like every other slice in the console: "spend, by user, last 30 days" is a thing
  // an operator pastes into a thread, and it was the one view that could not be linked to.
  const [params] = useSearchParams();
  const setParam = useUrlParam();
  const raw = params.get("by") ?? "";
  const group: Group = GROUPS.some((g) => g.key === raw) ? (raw as Group) : "total";
  const setGroup = (next: Group) => setParam("by", next === "total" ? "" : next);
  // One fetch per grouping, cached for the visit - flipping back is instant.
  const [byGroup, setByGroup] = useState<Partial<Record<Group, SpendPoint[]>>>({
    total: loaderData.daily,
  });

  useEffect(() => {
    if (byGroup[group]) return;
    let alive = true;
    void unwrap(
      api.GET("/v1/admin/spend/daily", {
        params: {
          query: {
            days: DAYS,
            ...(group === "total" ? {} : { group }),
          },
        },
      }),
    ).then(
      (points) => alive && setByGroup((cur) => ({ ...cur, [group]: points })),
      () => alive && setByGroup((cur) => ({ ...cur, [group]: [] })),
    );
    return () => {
      alive = false;
    };
  }, [group, byGroup]);

  const points = byGroup[group];

  return (
    <div className="space-y-6">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Tile label={`Last ${summary.days} days`}>
          <Money usd={summary.total_usd} />
        </Tile>
        <Tile label="This month">
          <Money usd={summary.mtd_usd} />
        </Tile>
        <Tile label="Today">
          <Money usd={summary.today_usd} />
        </Tile>
        <Tile label="Calls">
          <span className="tabular-nums">{summary.calls}</span>
        </Tile>
        {summary.zero_cost_calls > 0 && (
          <Tile label="Zero-cost calls" className="border-warning/30 bg-warning/5">
            <span className="tabular-nums">{summary.zero_cost_calls}</span>
            <span className="text-muted-foreground ml-2 text-caption-md-regular font-regular">
              missing a price row
            </span>
          </Tile>
        )}
      </div>

      <Section
        title="Daily"
        actions={
          <div className="flex items-center gap-2">
            {GROUPS.map((g) => (
              <ToggleChip key={g.key} active={group === g.key} onClick={() => setGroup(g.key)}>
                {g.label}
              </ToggleChip>
            ))}
          </div>
        }
      >
        {points === undefined ? (
          <LoadingState />
        ) : points.length === 0 ? (
          <EmptyState
            compact
            icon={Coins}
            title="No spend in the window"
            description="Provider calls land here as usage events with costs."
          />
        ) : (
          <DailyChart points={points} grouped={group !== "total"} />
        )}
      </Section>

      <Section title="By model">
        {summary.by_model.length === 0 ? (
          <EmptyState compact icon={Coins} title="No model spend yet" />
        ) : (
          <Rows as="ul">
            {summary.by_model.map((m) => (
              <li
                key={`${m.provider}:${m.model}`}
                className="flex items-center gap-3 px-4 py-3 text-body-sm-regular"
              >
                <span className="min-w-0 flex-1 truncate font-medium">
                  {m.provider}:{m.model}
                </span>
                <span className="text-muted-foreground shrink-0 text-caption-md-regular tabular-nums">
                  {m.calls} calls
                </span>
                <Money usd={m.cost_usd} className="shrink-0" />
              </li>
            ))}
          </Rows>
        )}
      </Section>

      <Section title="Top spenders">
        {summary.top_spenders.length === 0 ? (
          <EmptyState compact icon={Coins} title="No user spend yet" />
        ) : (
          <Rows as="ul">
            {summary.top_spenders.map((s) => (
              <li key={s.email}>
                {s.user_id ? (
                  <Link
                    to={`/users/${s.user_id}`}
                    className="hover:bg-accent/50 flex w-full items-center gap-3 px-4 py-3 text-body-sm-regular transition-colors"
                  >
                    <SpenderCells s={s} />
                  </Link>
                ) : (
                  <div className="flex items-center gap-3 px-4 py-3 text-body-sm-regular">
                    <SpenderCells s={s} />
                  </div>
                )}
              </li>
            ))}
          </Rows>
        )}
      </Section>

      <p className="text-muted-foreground flex flex-wrap gap-x-3 gap-y-1 text-caption-md-regular">
        {summary.price_versions.map((v) => (
          <MetaChip key={v.version} className="tabular-nums">
            price book v{v.version} · {v.calls} calls
          </MetaChip>
        ))}
      </p>
    </div>
  );
}

function SpenderCells({
  s,
}: {
  s: { email: string; name: string; calls: number; cost_usd: string };
}) {
  return (
    <>
      <span className="min-w-0 flex-1">
        <span className="block truncate font-medium">{s.email}</span>
        <span className="text-muted-foreground block truncate text-caption-md-regular">
          {s.name || "-"}
        </span>
      </span>
      <span className="text-muted-foreground shrink-0 text-caption-md-regular tabular-nums">
        {s.calls} calls
      </span>
      <Money usd={s.cost_usd} className="shrink-0" />
    </>
  );
}

function Tile({
  label,
  children,
  className,
}: {
  label: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <Card size="sm" className={className}>
      <CardContent>
        <p className="text-muted-foreground text-caption-md-regular font-medium tracking-wide uppercase">
          {label}
        </p>
        <p className="mt-1 text-body-sm-regular font-medium tabular-nums">{children}</p>
      </CardContent>
    </Card>
  );
}

const CHART_W = 640;
const CHART_H = 150;

/** Ungrouped: bars in primary, scaled to the day TOTAL. Grouped: one line per key off the hue
 *  ramp (rank = index - deterministic within a render; the legend beneath is the decoder),
 *  scaled to the tallest single SERIES-day. The lines do not stack, so they must not be
 *  measured against a stacked ceiling - that squashed every line by the number of keys. */
function DailyChart({ points, grouped }: { points: SpendPoint[]; grouped: boolean }) {
  const { days, series, stackedMax, seriesMax } = buildSpendSeries(points);
  if (days.length === 0) return null;
  const ceiling = grouped ? seriesMax : stackedMax;

  return (
    <div className="space-y-2">
      <svg
        viewBox={`0 0 ${CHART_W} ${CHART_H}`}
        preserveAspectRatio="none"
        className="h-40 w-full"
        role="img"
        aria-label={`Daily spend, ${days[0]} to ${days[days.length - 1]}`}
      >
        {grouped ? (
          series.map((s, i) => (
            <polyline
              key={s.key}
              points={linePoints(s.values, ceiling, CHART_W, CHART_H - 4)}
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              vectorEffect="non-scaling-stroke"
              className={
                s.key === "other" ? "text-muted-foreground" : HUE_TEXT[i % HUE_TEXT.length]
              }
            />
          ))
        ) : (
          <TotalBars points={points} />
        )}
      </svg>
      <div className="text-muted-foreground flex justify-between text-caption-md-regular tabular-nums">
        <span>{days[0]}</span>
        {/* The label names the ceiling actually drawn, so the axis and the words agree. */}
        <span>
          {grouped ? "busiest line" : "peak"} <Money usd={ceiling} muted />
          /day
        </span>
        <span>{days[days.length - 1]}</span>
      </div>
      {grouped && (
        <div className="flex flex-wrap gap-1.5">
          {series.map((s, i) => (
            <Pill
              key={s.key}
              className={cn(
                "max-w-64",
                s.key === "other" ? undefined : HUE_TINT[i % HUE_TINT.length],
              )}
            >
              <span className="truncate">{s.key}</span>
              <Money usd={s.total} className="shrink-0" />
            </Pill>
          ))}
        </div>
      )}
    </div>
  );
}

function TotalBars({ points }: { points: SpendPoint[] }) {
  const totals = dailyTotals(points);
  const max = Math.max(...totals.map((t) => t.value), 0);
  const slot = CHART_W / totals.length;
  const bar = Math.max(1, slot * 0.7);
  return (
    <g className="text-primary">
      {totals.map((t, i) => {
        const h = max > 0 ? (t.value / max) * (CHART_H - 4) : 0;
        return (
          <rect
            key={t.day}
            x={i * slot + (slot - bar) / 2}
            y={CHART_H - h}
            width={bar}
            height={h}
            fill="currentColor"
            rx={1}
          >
            <title>{`${t.day} - $${t.value.toFixed(4)}`}</title>
          </rect>
        );
      })}
    </g>
  );
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
