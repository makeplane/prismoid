/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The browser's data layer: group vocabulary, wire fetchers, the ONE param parser, and
 *  the client-side bucketing for non-status groupings. No JSX lives here. */

import { api, unwrap } from "~/lib/api/client";
import { dayKey, dayLabel } from "~/lib/format";
import { STORAGE } from "~/lib/storage";
import type { KeysetPage } from "~/lib/paging";
import type { components } from "~/lib/api/types.gen";

export type TaskSummary = components["schemas"]["TaskSummaryOut"];
export type ProjectRow = components["schemas"]["ProjectListOut"];
export type Counts = Record<string, number>;

export const PAGE = 25;
export const REFRESH_MS = 5000;

// The machine has nine states; people think in three. Each status group is its own
// independently-paginated server query - the groups never share a cursor.
export const TASK_GROUPS = [
  {
    key: "active",
    label: "In progress",
    dot: "running",
    statuses: ["created", "clarifying", "planning", "running", "waiting", "paused"],
  },
  {
    key: "delivered",
    label: "Delivered",
    dot: "delivered",
    statuses: ["delivered"],
  },
  {
    key: "closed",
    label: "Failed & cancelled",
    dot: "failed",
    statuses: ["failed", "cancelled"],
  },
] as const;
export type GroupDef = (typeof TASK_GROUPS)[number];
export type GroupKey = GroupDef["key"];

export const MODES = ["standard", "fleet"] as const;

// Grouping dimensions. Status groups query the server per group (true counts, per-group
// pagination); project/mode/date/month/none ride ONE filtered stream and bucket the loaded
// window - hundreds of projects (or days) cannot each be a query.
export type GroupBy = "status" | "project" | "mode" | "date" | "month" | "none";
export type View = "list" | "board";
export const VIEW_KEY = STORAGE.tasksView;

export interface TaskFilters {
  project: string | null;
  mode: string | null;
  statusKey: GroupKey | null;
  /** The archive drawer: default lists are the active world; true shows ONLY archived. */
  archived: boolean;
}

export interface TaskQueryScope {
  q: string;
  filters: TaskFilters;
  /** Hard scope (a project's Tasks tab) - every query carries it; the project dimension hides. */
  projectId?: string;
}

export const fetchTasksPage = (
  scope: TaskQueryScope,
  statuses: readonly string[] | undefined,
  cursor: string | null,
): Promise<KeysetPage<TaskSummary>> => {
  const project = scope.projectId ?? scope.filters.project;
  return unwrap(
    api.GET("/v1/tasks", {
      params: {
        query: {
          limit: PAGE,
          ...(statuses ? { status: [...statuses] } : {}),
          ...(project ? { project_id: project } : {}),
          ...(scope.filters.mode ? { mode: scope.filters.mode } : {}),
          ...(scope.filters.archived ? { archived: true } : {}),
          ...(scope.q ? { q: scope.q } : {}),
          ...(cursor ? { cursor } : {}),
        },
      },
    }),
  );
};

export const fetchTaskCounts = (scope: TaskQueryScope): Promise<Counts> => {
  const project = scope.projectId ?? scope.filters.project;
  return unwrap(
    api.GET("/v1/tasks/counts", {
      params: {
        query: {
          ...(project ? { project_id: project } : {}),
          ...(scope.filters.mode ? { mode: scope.filters.mode } : {}),
          ...(scope.filters.archived ? { archived: true } : {}),
          ...(scope.q ? { q: scope.q } : {}),
        },
      },
    }),
  ).then((r) => r.counts);
};

/** The ONE parser for browser params (the loader and the component each
 *  had their own). Takes a getter so URLSearchParams and component-local state both fit. */
export function readBrowserParams(get: (key: string) => string | null) {
  const groupParam = get("group");
  const statusParam = get("status");
  return {
    q: get("q") ?? "",
    group: (["status", "project", "mode", "date", "month", "none"] as const).includes(
      groupParam as GroupBy,
    )
      ? (groupParam as GroupBy)
      : ("status" as GroupBy),
    filters: {
      project: get("project"),
      mode: get("mode"),
      statusKey: TASK_GROUPS.some((g) => g.key === statusParam) ? (statusParam as GroupKey) : null,
      archived: get("archived") === "1",
    } satisfies TaskFilters,
  };
}

/** Loader seed - page one of whatever the mount state will ask for. */
export interface TaskBrowserSeed {
  q: string;
  pages: (KeysetPage<TaskSummary> | null)[] | null;
  stream: KeysetPage<TaskSummary> | null;
  counts: Counts | null;
}

/**
 * Every task list in the app, one browser: search, filters (status group · mode · project),
 * group-by (status · project · mode · date · month · none), and two layouts - List and Board.
 * Status grouping is per-group server queries with true GROUP BY counts; other groupings
 * bucket one filtered stream client-side. Boards refuse "none" and the calendar groupings:
 * a board's columns are categories, not days.
 *
 * `syncToUrl` mirrors state to search params (the /tasks page - a slice is shareable);
 * without it state is component-local (a project's Tasks tab). `projectId` hard-scopes every
 * query and hides the project dimension - inside a project, "which project" is not a question.
 */

// ── grouping ─────────────────────────────────────────────────────────────────────────────────

export interface Bucket {
  key: string;
  label: string;
  items: TaskSummary[];
}

const pad2 = (n: number) => String(n).padStart(2, "0");

/** Bucket the loaded window by the grouping dimension. Project/mode labels sort
 *  alphabetically with "No project" last - first-seen order would reshuffle on every head
 *  refresh; date/month buckets sort by their ISO-shaped keys, newest first, so chronology
 *  IS the sort and labels stay purely human (Today · Yesterday · 30 Jul · July 2026). */
export function bucketize(
  items: TaskSummary[],
  group: GroupBy,
  projectName: Map<string, string>,
): Bucket[] {
  if (group === "none") return [{ key: "all", label: "All", items }];
  if (group === "date" || group === "month") {
    const by = new Map<string, Bucket>();
    for (const t of items) {
      const d = new Date(t.created_at);
      const key = group === "date" ? dayKey(d) : `${d.getFullYear()}-${pad2(d.getMonth() + 1)}`;
      const bucket = by.get(key) ?? {
        key,
        label:
          group === "date"
            ? dayLabel(d)
            : d.toLocaleDateString(undefined, {
                month: "long",
                year: "numeric",
              }),
        items: [],
      };
      bucket.items.push(t);
      by.set(key, bucket);
    }
    return [...by.values()].sort((a, b) => b.key.localeCompare(a.key));
  }
  const by = new Map<string, Bucket>();
  for (const t of items) {
    const key = group === "project" ? (t.project_id ?? "__none") : t.mode || "standard";
    const label =
      group === "project"
        ? key === "__none"
          ? "No project"
          : (projectName.get(key) ?? "Project")
        : t.mode || "standard";
    const bucket = by.get(key) ?? { key, label, items: [] };
    bucket.items.push(t);
    by.set(key, bucket);
  }
  return [...by.values()].sort((a, b) =>
    a.key === "__none" ? 1 : b.key === "__none" ? -1 : a.label.localeCompare(b.label),
  );
}
