/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// One account, operationally - a DETAIL PAGE, not a summary card: the operator investigates
// here and never navigates away. Four tabs, tab in the URL (?tab=, overview default):
// Overview (status verbs, spend, the task-count chips - each chip flips to the Tasks tab
// with the filter set, same page), Tasks (the full TaskConsole scoped to this user, peek
// in place via ?task=), Access (their sessions and API keys, revocable per row), History
// (the audit trail filtered to this subject). Every verb confirms first (suspension takes
// a REASON - it lands in the audit row), lands with a toast, and refreshes the loader's
// truth rather than trusting an optimistic guess. Impossible verbs are ABSENT, not
// disabled: an active user shows Suspend, a suspended one shows Unsuspend.

import { ListTodo } from "lucide-react";
import { type ReactNode } from "react";
import { useLoaderData, useRevalidator, type ClientLoaderFunctionArgs } from "react-router";
import { revalidateOnPageChange } from "~/lib/pending-route";
import { toast } from "@prismoid/blocks/toast";
import { AccessTab } from "~/components/admin/access-tab";
import { AuditRows } from "~/components/admin/audit-rows";
import { ConfirmAction, ReasonDialog } from "~/components/admin/confirm";
import { Sparkline } from "~/components/admin/sparkline";
import { TaskConsole } from "~/components/admin/task-console";
import {
  EmptyState,
  initials,
  Money,
  PageHeader,
  Pill,
  Rows,
  Section,
  statusLabel,
} from "~/components/kit";
import { Badge } from "@makeplane/propel/components/badge";
import { Button as ButtonElement } from "@makeplane/propel/elements/button";
import { Tab as TabItem, Tabs, TabsList } from "@makeplane/propel/components/tabs";
import { api, unwrap } from "~/lib/api/client";
import { ago } from "~/lib/format";
import type { SpendPoint } from "~/lib/spend-series";
import { useUrlParams, useUrlTab } from "~/lib/url-state";

export function meta({ loaderData }: { loaderData?: { user?: { email?: string } } }) {
  return [
    {
      title: loaderData?.user ? `${loaderData.user.email} · Admin · Prismoid` : "User · Admin",
    },
  ];
}

export async function clientLoader({ params }: ClientLoaderFunctionArgs) {
  const id = params.id as string;
  const [user, spend] = await Promise.all([
    unwrap(api.GET("/v1/admin/users/{id}", { params: { path: { id } } })),
    unwrap(
      api.GET("/v1/admin/users/{id}/spend", {
        params: { path: { id }, query: { days: 30 } },
      }),
    ).catch(() => [] as SpendPoint[]),
  ]);
  return { user, spend };
}
clientLoader.hydrate = true as const;

// A tab, a filter or an open peek rides the query string: the page already holds its data, so
// the router does not hold that click for another fetch.
export const shouldRevalidate = revalidateOnPageChange;

// The breadcrumb says WHO, not "Detail".
export const handle = {
  crumb: (data: unknown) =>
    (data as { user?: { email?: string } } | undefined)?.user?.email ?? null,
};

const TABS = ["overview", "tasks", "access", "history"] as const;
type Tab = (typeof TABS)[number];

export default function AdminUserDetail() {
  const { user, spend } = useLoaderData<typeof clientLoader>();
  const revalidator = useRevalidator();
  const refresh = () => revalidator.revalidate();
  const suspended = user.status === "suspended";

  const [tab, setTab] = useUrlTab(TABS);
  const setParams = useUrlParams();
  // An Activity chip: same page, Tasks tab, filter set - ONE pushed entry, so Back returns
  // here rather than stepping through a half-applied state nobody was ever looking at.
  const openTasksWith = (status: string) => setParams({ tab: "tasks", status }, "push");

  return (
    <div className="space-y-6">
      <PageHeader
        leading={
          <span className="bg-muted text-muted-foreground flex size-10 shrink-0 items-center justify-center rounded-full text-body-sm-regular font-medium">
            {initials(user.name || user.email)}
          </span>
        }
        title={user.name || user.email}
        description={
          <span className="text-muted-foreground flex flex-wrap items-center gap-2 text-body-sm-regular">
            {user.email}
            <Pill tone={suspended ? "danger" : "success"} className="capitalize">
              {user.status}
            </Pill>
            {user.is_admin && <Badge variant="neutral" size="xs" label="admin" />}
            {user.deleted && <Badge variant="neutral" size="xs" label="deleted" />}· joined{" "}
            {ago(user.created_at)}
            {user.last_seen_at && <span>· last seen {ago(user.last_seen_at)}</span>}
          </span>
        }
      />

      {/* The negative margin sets the first label's text on the page edge. */}
      <div className="-ml-2.5">
        <Tabs variant="underline" value={tab} onValueChange={(v) => setTab(v as Tab)}>
          <TabsList>
            <TabItem value="overview" label="Overview" />
            <TabItem value="tasks" label="Tasks" />
            <TabItem value="access" label="Access" />
            <TabItem value="history" label="History" />
          </TabsList>
        </Tabs>
      </div>

      {tab === "overview" && (
        <>
          <Section title="Status & actions">
            <Rows as="ul">
              <ActionRow
                label={suspended ? "Unsuspend" : "Suspend"}
                hint={
                  suspended
                    ? "Let them back in - sessions stay revoked until they sign in again."
                    : "Locks the account out. The reason lands in the audit trail."
                }
              >
                {suspended ? (
                  <ConfirmAction
                    trigger="Unsuspend"
                    title={`Unsuspend ${user.email}?`}
                    description="They'll be able to sign in and run tasks again."
                    onConfirm={async () => {
                      await unwrap(
                        api.POST("/v1/admin/users/{id}/unsuspend", {
                          params: { path: { id: user.id } },
                        }),
                      );
                      toast.success("Unsuspended.");
                      refresh();
                    }}
                  />
                ) : (
                  <ReasonDialog
                    trigger="Suspend"
                    title={`Suspend ${user.email}?`}
                    description="They can't sign in or run tasks until unsuspended. The reason is recorded in the audit trail."
                    placeholder="Why this account is being locked out."
                    confirmLabel="Suspend"
                    onConfirm={async (reason) => {
                      await unwrap(
                        api.POST("/v1/admin/users/{id}/suspend", {
                          params: { path: { id: user.id } },
                          body: { reason },
                        }),
                      );
                      toast.success("Suspended.");
                      refresh();
                    }}
                  />
                )}
              </ActionRow>

              <ActionRow
                label={user.is_admin ? "Revoke admin" : "Grant admin"}
                hint={
                  user.is_admin
                    ? "Takes the operator console away from this account."
                    : "Gives this account the operator console - everything on these pages."
                }
              >
                <ConfirmAction
                  trigger={user.is_admin ? "Revoke admin" : "Grant admin"}
                  destructive={user.is_admin}
                  title={
                    user.is_admin
                      ? `Revoke admin from ${user.email}?`
                      : `Make ${user.email} an admin?`
                  }
                  description={
                    user.is_admin
                      ? "They keep their account; they lose the console."
                      : "Admins see every user, task, and ledger row. This is audited."
                  }
                  onConfirm={async () => {
                    await unwrap(
                      api.POST(
                        user.is_admin
                          ? "/v1/admin/users/{id}/revoke-admin"
                          : "/v1/admin/users/{id}/grant-admin",
                        { params: { path: { id: user.id } } },
                      ),
                    );
                    toast.success(user.is_admin ? "Admin revoked." : "Admin granted.");
                    refresh();
                  }}
                />
              </ActionRow>

              <ActionRow
                label="Sign out everywhere"
                hint={`${user.live_sessions} live session${user.live_sessions === 1 ? "" : "s"} · ${user.live_api_keys} API key${user.live_api_keys === 1 ? "" : "s"}`}
              >
                <ConfirmAction
                  trigger="Sign out everywhere"
                  destructive
                  title={`Revoke every credential for ${user.email}?`}
                  description="Ends all live sessions and API keys. Open streams sever within seconds."
                  onConfirm={async () => {
                    const out = await unwrap(
                      api.POST("/v1/admin/users/{id}/revoke-sessions", {
                        params: { path: { id: user.id } },
                      }),
                    );
                    toast.success(
                      `Revoked ${out.sessions_revoked} session${out.sessions_revoked === 1 ? "" : "s"} and ${out.api_keys_revoked} API key${out.api_keys_revoked === 1 ? "" : "s"}.`,
                    );
                    refresh();
                  }}
                />
              </ActionRow>
            </Rows>
          </Section>

          <Section title="Activity">
            {Object.keys(user.task_counts).length === 0 ? (
              <EmptyState
                compact
                icon={ListTodo}
                title="No tasks yet"
                description="What they run shows up here, by status."
              />
            ) : (
              <div className="flex flex-wrap gap-2">
                {Object.entries(user.task_counts).map(([status, count]) => (
                  // ButtonElement: the status and its muted count are two styled runs.
                  <ButtonElement
                    key={status}
                    type="button"
                    variant="secondary"
                    size="sm"
                    stretch="auto"
                    onClick={() => openTasksWith(status)}
                  >
                    <span className="capitalize">{statusLabel(status)}</span>
                    <span className="text-muted-foreground tabular-nums">{count}</span>
                  </ButtonElement>
                ))}
              </div>
            )}
          </Section>

          <Section title="Spend">
            <Rows as="div">
              <div className="space-y-3 px-4 py-3.5">
                <p className="text-muted-foreground text-body-sm-regular">
                  <Money usd={user.spend_mtd_usd} className="text-foreground" /> this month ·{" "}
                  <Money usd={user.spend_total_usd} className="text-foreground" /> all time
                </p>
                <Sparkline points={spend} />
              </div>
            </Rows>
          </Section>
        </>
      )}

      {tab === "tasks" && <TaskConsole userId={user.id} />}

      {tab === "access" && <AccessTab userId={user.id} onRevoked={refresh} />}

      {tab === "history" && (
        <AuditRows
          subjectType="user"
          subjectId={user.id}
          emptyTitle="No admin actions on this account yet"
          emptyDescription="Suspensions, grants, and revocations against this user land here."
        />
      )}
    </div>
  );
}

/** A verb row: what it does on the left, the (dialog-guarded) control on the right. */
function ActionRow({
  label,
  hint,
  children,
}: {
  label: string;
  hint: string;
  children: ReactNode;
}) {
  return (
    <li className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2 px-4 py-3.5">
      <span className="min-w-0">
        <span className="block text-body-sm-regular font-medium">{label}</span>
        <span className="text-muted-foreground block text-caption-md-regular">{hint}</span>
      </span>
      <span className="flex shrink-0 items-center gap-2">{children}</span>
    </li>
  );
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
