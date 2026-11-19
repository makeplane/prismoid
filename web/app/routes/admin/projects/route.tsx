/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Every project in the system, observed - a ResourceList config: name + owner on the left,
// the operational trio on the right (members · tasks · MTD spend against the budget when
// one is set). Read-only by design: the operator watches budgets and membership here and
// acts through the owner, never over their head. A row opens the project's detail page.
// The wire supports exactly q (name substring, or a pasted project UUID) - nothing faked.

import { FolderKanban } from "lucide-react";
import { ResourceList, type ResourceListConfig } from "~/components/admin/resource-list";
import { IconTile, Money } from "~/components/kit";
import { api, unwrap } from "~/lib/api/client";
import { ago } from "~/lib/format";
import type { components } from "~/lib/api/types.gen";

type ProjectRow = components["schemas"]["AdminProjectOut"];

export function meta() {
  return [{ title: "Projects · Admin · Prismoid" }];
}

const config: ResourceListConfig<ProjectRow> = {
  fetchPage: ({ cursor, q, limit }) =>
    unwrap(
      api.GET("/v1/admin/projects", {
        params: {
          query: {
            limit,
            ...(q ? { q } : {}),
            ...(cursor ? { cursor } : {}),
          },
        },
      }),
    ),
  orderKey: (s) => s.created_at,
  rowKey: (s) => s.id,
  rowTo: (s) => `/projects/${s.id}`,
  row: (s) => (
    <>
      <IconTile icon={FolderKanban} />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-body-sm-regular font-medium">{s.name}</span>
        <span className="text-muted-foreground block truncate text-caption-md-regular">
          {s.owner ? s.owner.email : "no owner"} · created {ago(s.created_at)}
        </span>
      </span>
      <span className="text-muted-foreground shrink-0 text-caption-md-regular tabular-nums">
        {s.members} member{s.members === 1 ? "" : "s"} · {s.tasks} task
        {s.tasks === 1 ? "" : "s"} · <Money usd={s.mtd_usd} />
        {s.budget_usd != null && (
          <>
            {" of "}
            <Money usd={s.budget_usd} />
          </>
        )}
      </span>
    </>
  ),
  search: { placeholder: "Search project name or ID…", topbar: true },
  empty: {
    icon: FolderKanban,
    title: "No projects",
    description: "Shared projects appear here as people create them.",
  },
};

export default function AdminProjects() {
  return <ResourceList {...config} />;
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
