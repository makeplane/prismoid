/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// One workspace, operationally: its standing and its audit trail. The wire gives the operator
// the workspace's summary (member count) and its audit trail - not a roster, which is the
// members' own view.

import { capitalize } from "~/lib/utils";
import { useLoaderData, type ClientLoaderFunctionArgs } from "react-router";
import { AuditRows } from "~/components/admin/audit-rows";
import { PageHeader, Pill, Rows, Section } from "~/components/kit";
import { Badge } from "@makeplane/propel/components/badge";
import { api, unwrap } from "~/lib/api/client";
import { ago } from "~/lib/format";

export function meta({ loaderData }: { loaderData?: { workspace?: { name?: string } } }) {
  return [{ title: `${loaderData?.workspace?.name ?? "Workspace"} · Admin · Prismoid` }];
}

export async function clientLoader({ params }: ClientLoaderFunctionArgs) {
  const workspace_id = params.id as string;
  const workspace = await unwrap(
    api.GET("/v1/admin/workspaces/{workspace_id}", { params: { path: { workspace_id } } }),
  );
  return { workspace };
}
clientLoader.hydrate = true as const;

export const handle = {
  crumb: (data: unknown) =>
    (data as { workspace?: { name?: string } } | undefined)?.workspace?.name ?? null,
};

export default function AdminWorkspaceDetail() {
  const { workspace } = useLoaderData<typeof clientLoader>();

  return (
    <div className="space-y-6">
      <PageHeader
        title={workspace.name}
        description={
          <span className="text-muted-foreground flex flex-wrap items-center gap-2 text-body-sm-regular">
            {workspace.slug}
            <Badge variant="neutral" size="xs" label={capitalize(workspace.kind)} />
            <Pill
              tone={workspace.status === "active" ? "success" : "danger"}
              className="capitalize"
            >
              {workspace.status}
            </Pill>
            · created {ago(workspace.created_at)}
          </span>
        }
      />
      <Section title="Standing">
        <Rows as="div">
          <div className="flex items-center justify-between px-4 py-3.5 text-body-sm-regular">
            <span className="text-muted-foreground">Members</span>
            <span className="tabular-nums">{workspace.members}</span>
          </div>
        </Rows>
      </Section>
      <Section title="History">
        <AuditRows
          subjectType="workspace"
          subjectId={workspace.id}
          emptyTitle="No operator actions on this workspace yet"
          emptyDescription="Operator actions against this workspace land here."
        />
      </Section>
    </div>
  );
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
