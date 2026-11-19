/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The Runs and Artifacts tabs - the bot's history and its shelf (ADR-045). Both are quiet
// read surfaces: fetch once per open, rows in the app's list grammar. A row opens IN PLACE -
// the task peek / reader drawer over the messenger (the inbox law: read it where you are;
// navigation away is the peek's own escalation, never the row's).

import { useEffect, useState } from "react";
import { ArtifactReader } from "~/components/artifact/artifact-reader";
import { EmptyState, LoadingState, Pill, StatusDot, VerdictBadge } from "~/components/kit";
import { api, toastError, unwrap } from "~/lib/api/client";
import { ago } from "~/lib/format";
import type { components } from "~/lib/api/types.gen";
import { TaskPeek } from "~/components/tasks/task-peek";

type RunOut = components["schemas"]["BotRunOut"];
type ArtifactOut = components["schemas"]["BotArtifactOut"];
type StatsOut = components["schemas"]["BotStatsOut"];

function useFetch<T>(load: () => Promise<T>, deps: unknown[]): T | undefined {
  const [data, setData] = useState<T | undefined>(undefined);
  // oxlint-disable-next-line react-hooks/exhaustive-deps -- `deps` is the caller's list, passed through
  useEffect(() => {
    let alive = true;
    setData(undefined);
    load().then(
      (d) => alive && setData(d),
      (e) => alive && toastError(e, "Couldn't load this tab."),
    );
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `deps` is the caller's list, passed through
  }, deps);
  return data;
}

/** The stats strip: spend and runs by join, version-blind by name and honest
 *  about it. Quiet meta text, never a dashboard - the numbers speak once, above the rows. */
function StatsStrip({ name }: { name: string }) {
  const stats = useFetch<StatsOut>(
    () => unwrap(api.GET("/v1/bots/{name}/stats", { params: { path: { name } } })),
    [name],
  );
  if (!stats || stats.runs === 0) return null;
  const usd = (v: string) => `$${Number(v).toFixed(2)}`;
  return (
    <p className="text-muted-foreground pb-3 text-caption-md-regular tabular-nums">
      {usd(stats.spend_usd_30d)} last 30 days · {usd(stats.spend_usd_total)} all time · {stats.runs}{" "}
      {stats.runs === 1 ? "run" : "runs"} as {name}
      {stats.memory_count > 0 && ` · ${stats.memory_count} memories`}
      {stats.routine_count > 0 && ` · ${stats.routine_count} routines`}
    </p>
  );
}

export function RunsTab({ name }: { name: string }) {
  const [peek, setPeek] = useState<string | null>(null);
  const runs = useFetch<RunOut[]>(
    () => unwrap(api.GET("/v1/bots/{name}/runs", { params: { path: { name } } })),
    [name],
  );
  if (runs === undefined)
    return (
      <div className="px-6 py-4">
        <LoadingState label="Loading runs…" />
      </div>
    );
  if (runs.length === 0)
    return (
      <div className="px-6 py-4">
        <EmptyState
          title="No runs yet"
          description="Every step this bot serves (from its chat or a routed task) lands here with its cost."
        />
      </div>
    );
  return (
    <div className="scrollbar-thin min-h-0 flex-1 overflow-y-auto px-6 py-4">
      <StatsStrip name={name} />
      <ul className="ring-foreground/10 overflow-hidden rounded-md ring-1">
        {runs.map((r, i) => (
          <li key={`${r.task_id}-${i}`}>
            <button
              type="button"
              onClick={() => setPeek(r.task_id)}
              className="border-sidebar-border/60 hover:bg-accent/40 flex w-full items-center gap-3 border-b px-4 py-3 text-left text-body-sm-regular transition-colors last:border-b-0"
            >
              <StatusDot status={r.state} decorative />
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium">{r.title}</span>
                {/* the step title is usually the goal (sometimes capped) - echoing it is noise */}
                {!r.goal.startsWith(r.title) && !r.title.startsWith(r.goal) && (
                  <span className="text-muted-foreground block truncate text-caption-md-regular">
                    {r.goal}
                  </span>
                )}
              </span>
              {r.attempt > 1 && <Pill tone="warning">×{r.attempt}</Pill>}
              <span className="text-muted-foreground shrink-0 text-caption-md-regular tabular-nums">
                ${r.cost_usd}
              </span>
              <span className="text-muted-foreground w-10 shrink-0 text-right text-caption-md-regular">
                {ago(r.at)}
              </span>
            </button>
          </li>
        ))}
      </ul>
      <TaskPeek taskId={peek} onClose={() => setPeek(null)} />
    </div>
  );
}

export function ArtifactsTab({ name }: { name: string }) {
  const [peek, setPeek] = useState<string | null>(null);
  const artifacts = useFetch<ArtifactOut[]>(
    () => unwrap(api.GET("/v1/bots/{name}/artifacts", { params: { path: { name } } })),
    [name],
  );
  if (artifacts === undefined)
    return (
      <div className="px-6 py-4">
        <LoadingState label="Loading artifacts…" />
      </div>
    );
  if (artifacts.length === 0)
    return (
      <div className="px-6 py-4">
        <EmptyState
          title="Nothing published yet"
          description="Everything this bot publishes (from its chat and from routed tasks) lands here with its verification verdict."
        />
      </div>
    );
  return (
    <div className="scrollbar-thin min-h-0 flex-1 overflow-y-auto px-6 py-4">
      <ul className="ring-foreground/10 overflow-hidden rounded-md ring-1">
        {artifacts.map((a) => (
          <li key={a.id}>
            <button
              type="button"
              onClick={() => setPeek(a.id)}
              className="border-sidebar-border/60 hover:bg-accent/40 flex w-full items-center gap-3 border-b px-4 py-3 text-left text-body-sm-regular transition-colors last:border-b-0"
            >
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium">{a.filename}</span>
                <span className="text-muted-foreground block truncate text-caption-md-regular">
                  {a.type} · from “{a.goal}”
                </span>
              </span>
              <VerdictBadge verdict={a.verdict} />
              <span className="text-muted-foreground w-10 shrink-0 text-right text-caption-md-regular">
                {ago(a.at)}
              </span>
            </button>
          </li>
        ))}
      </ul>
      <p className="text-muted-foreground pt-2.5 text-caption-md-regular">
        From its chat and from tasks routed to it - it opens right here, with the full report.
      </p>
      {/* The same resizable reader every shelf opens (the projects precedent, verbatim). */}
      <ArtifactReader
        artifactId={peek}
        onClose={() => setPeek(null)}
        kindHint={artifacts.find((a) => a.id === peek)?.type}
        onArtifactChanged={setPeek}
      />
    </div>
  );
}
