/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Every account, searchable - the first speaker of the admin ResourceList engine
// (components/admin/resource-list): the route states WHAT (the typed fetch, the row's
// content, the one status filter the wire supports - nothing faked), the engine owns HOW
// (topbar search, URL-borne state, keyset LoadMore, empty/loading states). A row click
// opens the user's operator view.

import { Users } from "lucide-react";
import { ResourceList, type ResourceListConfig } from "~/components/admin/resource-list";
import { initials, Pill } from "~/components/kit";
import { Badge } from "@makeplane/propel/components/badge";
import { api, unwrap } from "~/lib/api/client";
import { ago } from "~/lib/format";
import type { components } from "~/lib/api/types.gen";

type UserRow = components["schemas"]["AdminUserOut"];

export function meta() {
  return [{ title: "Users · Admin · Prismoid" }];
}

const config: ResourceListConfig<UserRow> = {
  fetchPage: ({ cursor, q, filters, limit }) =>
    unwrap(
      api.GET("/v1/admin/users", {
        params: {
          query: {
            limit,
            ...(q ? { q } : {}),
            ...(filters.status ? { status: filters.status } : {}),
            ...(cursor ? { cursor } : {}),
          },
        },
      }),
    ),
  orderKey: (u) => u.created_at,
  rowKey: (u) => u.id,
  rowTo: (u) => `/users/${u.id}`,
  row: (u) => (
    <>
      <span className="bg-muted text-muted-foreground flex size-8 shrink-0 items-center justify-center rounded-full text-caption-md-regular font-medium">
        {initials(u.name || u.email)}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-body-sm-regular font-medium">{u.email}</span>
        <span className="text-muted-foreground block truncate text-caption-md-regular">
          {u.name || "-"} · joined {ago(u.created_at)}
        </span>
      </span>
      {u.is_admin && <Badge variant="neutral" size="xs" label="admin" />}
      {u.deleted && <Badge variant="neutral" size="xs" label="deleted" />}
      <Pill tone={u.status === "suspended" ? "danger" : "success"} className="capitalize">
        {u.status}
      </Pill>
    </>
  ),
  search: { placeholder: "Search email, name, or ID…", topbar: true },
  filters: [
    {
      param: "status",
      options: [
        { value: "active", label: "Active" },
        { value: "suspended", label: "Suspended" },
      ],
    },
  ],
  empty: {
    icon: Users,
    title: "No users",
    description: "Accounts appear here as people sign up.",
  },
};

export default function AdminUsers() {
  return <ResourceList {...config} />;
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
