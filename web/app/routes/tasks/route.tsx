/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { Plus } from "lucide-react";
import { Link } from "react-router";
import { TopbarPortal } from "~/components/layout/topbar-slot";
import {
  fetchTaskCounts,
  fetchTasksPage,
  readBrowserParams,
  TASK_GROUPS,
  TaskBrowser,
  type TaskBrowserSeed,
} from "~/components/tasks/task-browser";
import { api, unwrap } from "~/lib/api/client";
import { NEW_TASK_HREF } from "~/lib/links";
import type { components } from "~/lib/api/types.gen";
import type { Route } from "./+types/route";

type ProjectRow = components["schemas"]["ProjectListOut"];

export function meta() {
  return [{ title: "Tasks · Prismoid" }];
}

export async function clientLoader({ request }: Route.ClientLoaderArgs) {
  const params = new URL(request.url).searchParams;
  const { q, group, filters } = readBrowserParams((k) => params.get(k));
  const scope = { q, filters };
  const statusFilter = TASK_GROUPS.find((g) => g.key === filters.statusKey);

  const [pages, stream, counts, projects] = await Promise.all([
    group === "status"
      ? Promise.all(
          TASK_GROUPS.map((g) =>
            !filters.statusKey || filters.statusKey === g.key
              ? fetchTasksPage(scope, g.statuses, null)
              : Promise.resolve(null),
          ),
        )
      : Promise.resolve(null),
    group !== "status"
      ? fetchTasksPage(scope, statusFilter?.statuses, null)
      : Promise.resolve(null),
    fetchTaskCounts(scope),
    unwrap(api.GET("/v1/projects", { params: { query: { limit: 100 } } })).then(
      (p) => p.items,
      () => [] as ProjectRow[],
    ),
  ]);
  const seed: TaskBrowserSeed = { q, pages, stream, counts };
  return { seed, projects };
}
clientLoader.hydrate = true as const;

// Search, filters, grouping and view are client-managed after hydration - a URL-param edit
// must not re-run the loader's fetches on every debounced keystroke.
export function shouldRevalidate() {
  return false;
}

export default function Tasks({ loaderData }: Route.ComponentProps) {
  return (
    // No in-page title: the breadcrumb IS the page's name (the section-tab pattern,
    // see section-tab.tsx) - search + the primary action ride the topbar instead. Tree order
    // is the slot order: the browser (and its search portal) mounts BEFORE the New-task
    // portal, so the header reads [Search] [+ New task]. No wrapper stack: the browser's
    // own toolbar-row + space-y-4 IS the SectionTab anatomy.
    <>
      <TaskBrowser syncToUrl searchInTopbar projects={loaderData.projects} seed={loaderData.seed} />
      <TopbarPortal>
        <Button
          nativeButton={false}
          render={<Link to={NEW_TASK_HREF} />}
          variant="primary"
          size="sm"
          stretch="auto"
          icon={<Icon icon={Plus} />}
          label="New task"
        />
      </TopbarPortal>
    </>
  );
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
