/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Every workspace, keyset-paged - the ResourceList engine speaks it (the wire has no search
// or filter for workspaces, so none is faked). A row opens the workspace's operator view.

import { capitalize } from "~/lib/utils";
import { Badge } from "@makeplane/propel/components/badge";
import { Building2 } from "lucide-react";
import { ResourceList, type ResourceListConfig } from "~/components/admin/resource-list";
import { Pill } from "~/components/kit";
import { api, unwrap } from "~/lib/api/client";
import type { components } from "~/lib/api/types.gen";
import { ago } from "~/lib/format";

type WorkspaceRow = components["schemas"]["AdminWorkspaceOut"];

export function meta() {
  return [{ title: "Workspaces · Admin · Prismoid" }];
}

const config: ResourceListConfig<WorkspaceRow> = {
  fetchPage: ({ cursor, limit }) =>
    unwrap(
      api.GET("/v1/admin/workspaces", {
        params: { query: { limit, ...(cursor ? { cursor } : {}) } },
      }),
    ),
  orderKey: (o) => o.created_at,
  rowKey: (o) => o.id,
  rowTo: (o) => `/workspaces/${o.id}`,
  row: (o) => (
    <>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-body-sm-regular font-medium">{o.name}</span>
        <span className="text-muted-foreground block truncate text-caption-md-regular">
          {o.slug} · {o.members} member{o.members === 1 ? "" : "s"} · created {ago(o.created_at)}
        </span>
      </span>
      <Badge variant="neutral" size="xs" label={capitalize(o.kind)} />
      <Pill tone={o.status === "active" ? "success" : "danger"} className="capitalize">
        {o.status}
      </Pill>
    </>
  ),
  empty: {
    icon: Building2,
    title: "No workspaces",
    description: "Workspaces appear here as people sign up.",
  },
};

export default function AdminWorkspaces() {
  return <ResourceList {...config} />;
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
