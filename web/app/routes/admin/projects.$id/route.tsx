/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// One project, operationally - a read-only detail page: the operator observes membership and
// spend here and never mutates a project over its owner's head (no verbs - capability law:
// absent, not disabled). Three tabs, tab in the URL (?tab=, overview default): Overview
// (the counts and the budget-vs-MTD line), Members (owner first, each email a link to the
// account's operator page), Tasks (the full TaskConsole hard-scoped to this project, peek in
// place via ?task=).

import { capitalize } from "~/lib/utils";
import { Badge } from "@makeplane/propel/components/badge";
import { FolderKanban, Users } from "lucide-react";
import { Link, useLoaderData, type ClientLoaderFunctionArgs } from "react-router";
import { revalidateOnPageChange } from "~/lib/pending-route";
import { TaskConsole } from "~/components/admin/task-console";
import { EmptyState, IconTile, initials, Money, PageHeader, Rows, Section } from "~/components/kit";
import { Tab as TabItem, Tabs, TabsList } from "@makeplane/propel/components/tabs";
import { api, unwrap } from "~/lib/api/client";
import { ago } from "~/lib/format";
import { useUrlTab } from "~/lib/url-state";

export function meta({ loaderData }: { loaderData?: { project?: { name?: string } } }) {
  return [
    {
      title: loaderData?.project
        ? `${loaderData.project.name} · Admin · Prismoid`
        : "Project · Admin",
    },
  ];
}

export async function clientLoader({ params }: ClientLoaderFunctionArgs) {
  const project = await unwrap(
    api.GET("/v1/admin/projects/{project_id}", {
      params: { path: { project_id: params.id as string } },
    }),
  );
  return { project };
}
clientLoader.hydrate = true as const;

// A tab, a filter or an open peek rides the query string: the page already holds its data, so
// the router does not hold that click for another fetch.
export const shouldRevalidate = revalidateOnPageChange;

// The breadcrumb says WHICH project, not "Detail".
export const handle = {
  crumb: (data: unknown) =>
    (data as { project?: { name?: string } } | undefined)?.project?.name ?? null,
};

const TABS = ["overview", "members", "tasks"] as const;
type Tab = (typeof TABS)[number];

export default function AdminProjectDetail() {
  const { project } = useLoaderData<typeof clientLoader>();

  const [tab, setTab] = useUrlTab(TABS);

  return (
    <div className="space-y-6">
      <PageHeader
        leading={<IconTile icon={FolderKanban} className="size-10" />}
        title={project.name}
        description={
          <span className="text-muted-foreground flex flex-wrap items-center gap-2 text-body-sm-regular">
            {project.owner ? (
              <Link
                to={`/users/${project.owner.id}`}
                className="hover:text-foreground hover:underline"
              >
                {project.owner.email}
              </Link>
            ) : (
              "no owner"
            )}
            · created {ago(project.created_at)}
          </span>
        }
      />

      {/* The negative margin sets the first label's text on the page edge. */}
      <div className="-ml-2.5">
        <Tabs variant="underline" value={tab} onValueChange={(v) => setTab(v as Tab)}>
          <TabsList>
            <TabItem value="overview" label="Overview" />
            <TabItem value="members" label="Members" />
            <TabItem value="tasks" label="Tasks" />
          </TabsList>
        </Tabs>
      </div>

      {tab === "overview" && (
        <Section title="Facts">
          <Rows as="ul">
            <FactRow label="Members">
              {project.members} member{project.members === 1 ? "" : "s"}
            </FactRow>
            <FactRow label="Tasks">
              {project.tasks} task{project.tasks === 1 ? "" : "s"}
            </FactRow>
            <FactRow label="Spend this month">
              <Money usd={project.mtd_usd} className="text-foreground" />
              {project.budget_usd != null ? (
                <>
                  {" of a "}
                  <Money usd={project.budget_usd} className="text-foreground" />
                  {" budget"}
                </>
              ) : (
                " · no budget set"
              )}
            </FactRow>
          </Rows>
        </Section>
      )}

      {tab === "members" &&
        (project.member_list.length === 0 ? (
          <EmptyState
            icon={Users}
            title="No members"
            description="The people in this project appear here, owner first."
          />
        ) : (
          <Rows as="ul">
            {project.member_list.map((m) => (
              <li key={m.id} className="flex items-center gap-3 px-4 py-3">
                <span className="bg-muted text-muted-foreground flex size-8 shrink-0 items-center justify-center rounded-full text-caption-md-regular font-medium">
                  {initials(m.name || m.email)}
                </span>
                <span className="min-w-0 flex-1">
                  <Link
                    to={`/users/${m.id}`}
                    className="block truncate text-body-sm-regular font-medium hover:underline"
                  >
                    {m.email}
                  </Link>
                  <span className="text-muted-foreground block truncate text-caption-md-regular">
                    {m.name || "-"}
                    {m.added_at && ` · added ${ago(m.added_at)}`}
                  </span>
                </span>
                <Badge variant="neutral" size="xs" label={capitalize(m.role)} />
              </li>
            ))}
          </Rows>
        ))}

      {tab === "tasks" && <TaskConsole projectId={project.id} />}
    </div>
  );
}

/** One quiet fact: the label left, the figure right. */
function FactRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <li className="flex flex-wrap items-center justify-between gap-x-6 gap-y-1 px-4 py-3">
      <span className="text-body-sm-regular font-medium">{label}</span>
      <span className="text-muted-foreground text-body-sm-regular tabular-nums">{children}</span>
    </li>
  );
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
