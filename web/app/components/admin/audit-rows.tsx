/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The audit trail's rows - ONE rendering, two homes: the global /audit page (unfiltered)
// and a user page's History tab (scoped by subject). Reading is the whole surface - rows
// are written by the backend as verbs happen, never from here. Subjects the console has a
// page for become links: a user subject opens their operator page, a task subject opens
// the /tasks peek in place (?task=). Everything else stays a plain fact.

import { ScrollText } from "lucide-react";
import { Link } from "react-router";
import { EmptyState, LoadingState, LoadMore, MetaChip, Rows } from "~/components/kit";
import { Badge } from "@makeplane/propel/components/badge";
import { api, unwrap } from "~/lib/api/client";
import { ago } from "~/lib/format";
import { useKeysetList } from "~/lib/paging";
import type { components } from "~/lib/api/types.gen";

type ActionRow = components["schemas"]["AdminActionOut"];

const PAGE = 50;

export function AuditRows({
  subjectType,
  subjectId,
  emptyTitle = "No admin actions yet",
  emptyDescription = "Suspensions, cancels, grants, and reprices land here as they happen.",
}: {
  /** Scope the trail to one subject - both or neither. */
  subjectType?: string;
  subjectId?: string;
  emptyTitle?: string;
  emptyDescription?: string;
}) {
  const { items, loadMore, loadingMore, hasMore } = useKeysetList<ActionRow>({
    fetchPage: (cursor) =>
      unwrap(
        api.GET("/v1/admin/audit", {
          params: {
            query: {
              limit: PAGE,
              ...(subjectType ? { subject_type: subjectType } : {}),
              ...(subjectId ? { subject_id: subjectId } : {}),
              ...(cursor ? { cursor } : {}),
            },
          },
        }),
      ),
    deps: [subjectType, subjectId],
    orderKey: (a) => a.at,
  });

  return (
    <div className="space-y-3">
      {items === null ? (
        <LoadingState />
      ) : items.length === 0 ? (
        <EmptyState icon={ScrollText} title={emptyTitle} description={emptyDescription} />
      ) : (
        <>
          <Rows as="ul">
            {items.map((a) => {
              const reason = a.detail?.reason;
              return (
                <li key={a.id} className="flex items-start gap-3 px-4 py-3">
                  <span className="text-muted-foreground w-16 shrink-0 pt-0.5 text-caption-md-regular tabular-nums">
                    {ago(a.at)}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex flex-wrap items-center gap-2">
                      {/* A null admin does NOT mean the system did this: it means the operator's
                          User row was erased (GDPR) while the Actor - and this row - survived,
                          which is the whole point of the Actor/User split. Saying "system" on
                          the one screen whose job is attribution attributed a person's act to
                          the machine. */}
                      <span className="truncate text-body-sm-regular font-medium">
                        {a.admin ? a.admin.email : "erased account"}
                      </span>
                      <Badge variant="neutral" size="xs" label={a.action} />
                      <Subject type={a.subject_type} id={a.subject_id} />
                    </span>
                    {typeof reason === "string" && reason && (
                      <span className="text-muted-foreground mt-0.5 block text-caption-md-regular">
                        {reason}
                      </span>
                    )}
                  </span>
                </li>
              );
            })}
          </Rows>
          <LoadMore hasMore={hasMore} busy={loadingMore} onClick={() => void loadMore()} />
        </>
      )}
    </div>
  );
}

/** The subject chip - a link when the console has a surface for it, plain text otherwise. */
function Subject({ type, id }: { type: string; id: string }) {
  const to =
    type === "user"
      ? `/users/${id}`
      : type === "task"
        ? `/tasks?task=${id}`
        : type === "project"
          ? `/projects/${id}`
          : type === "bot"
            ? `/bots/${id}`
            : null;
  return (
    <MetaChip className="tabular-nums">
      {to ? (
        <Link to={to} className="hover:text-foreground hover:underline">
          {type}:{id}
        </Link>
      ) : (
        <>
          {type}:{id}
        </>
      )}
    </MetaChip>
  );
}
