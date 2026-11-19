/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The operator's task console - ONE component, three homes: the /tasks route (global,
// search in the topbar), a user page's Tasks tab (scoped by `userId`, search inline), and
// a project page's Tasks tab (scoped by `projectId`, same anatomy). Filters
// are exactly what the wire supports (status multi, stuck, free text - the search also
// exact-matches a pasted task UUID); the head re-polls every 10s via useKeysetList so
// status flips surface without a reload. All state is URL-borne so a slice is shareable
// and composes with the host page's own params. A row click PEEKS the task in place as
// `?task=<id>` - pushed, so Back closes it; a direct load with the param present opens the
// peek immediately. The operator investigates here and never navigates away.

import { ListTodo, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";
import {
  EmptyState,
  LoadingState,
  LoadMore,
  Money,
  Rows,
  SearchBox,
  StatusMark,
  statusLabel,
  ToggleChip,
} from "~/components/kit";
import { TopbarPortal } from "~/components/layout/topbar-slot";
import { api, unwrap } from "~/lib/api/client";
import { ago } from "~/lib/format";
import { useDebouncedValue, useKeysetList } from "~/lib/paging";
import { useUrlParam } from "~/lib/url-state";
import type { components } from "~/lib/api/types.gen";
import { AdminTaskPeek } from "./admin-peek";

export type AdminTaskRow = components["schemas"]["AdminTaskOut"];

const PAGE = 30;

/** A scope chip wears the thing's NAME. Best-effort and module-scoped so the resolving hook's
 *  effect has a stable dependency: the id stands in until the label lands, and stands for good
 *  if the fetch fails - a chip that says nothing is worse than a chip that says the id. */
const fetchUserEmail = (id: string) =>
  unwrap(api.GET("/v1/admin/users/{id}", { params: { path: { id } } })).then((u) => u.email);
const fetchProjectName = (id: string) =>
  unwrap(api.GET("/v1/admin/projects/{project_id}", { params: { path: { project_id: id } } })).then(
    (s) => s.name,
  );

function useLinkedLabel(
  id: string | null,
  fetchLabel: (id: string) => Promise<string>,
): string | null {
  const [label, setLabel] = useState<string | null>(null);
  useEffect(() => {
    setLabel(null);
    if (!id) return;
    let alive = true;
    void fetchLabel(id).then(
      (value) => {
        if (alive) setLabel(value);
      },
      () => {},
    );
    return () => {
      alive = false;
    };
  }, [id, fetchLabel]);
  return label;
}

const STATUSES = [
  "created",
  "clarifying",
  "planning",
  "running",
  "waiting",
  "paused",
  "delivered",
  "failed",
  "cancelled",
] as const;

export function TaskConsole({
  userId,
  projectId,
  topbarSearch,
}: {
  /** Hard scope: every fetch is this user's, and no user chip renders. */
  userId?: string;
  /** Hard scope: every fetch is this project's, and no project chip renders. */
  projectId?: string;
  /** The /tasks route projects its search into the topbar; embedded copies keep it inline. */
  topbarSearch?: boolean;
}) {
  const [params] = useSearchParams();
  const set = useUrlParam();
  const q = params.get("q") ?? "";
  const dq = useDebouncedValue(q.trim(), 250);
  const statuses = (params.get("status") ?? "").split(",").filter(Boolean);
  const stuck = params.get("stuck") === "true";
  // Link-borne scoping (a chip you can clear) exists only on the global console - an
  // embedded console is already scoped by its host page.
  const linkedUserId = userId ? null : params.get("user_id");
  const linkedProjectId = projectId ? null : params.get("project_id");
  const peekId = params.get("task");

  // Open PUSHES (Back closes the peek); close REPLACES (Back doesn't reopen it).
  const openPeek = (id: string) => set("task", id, "push");
  const closePeek = () => set("task", "");

  const toggleStatus = (s: string) => {
    const next = statuses.includes(s) ? statuses.filter((x) => x !== s) : [...statuses, s];
    set("status", next.join(","));
  };

  // Both scope chips say WHAT they scope to, not "one user" / "one project".
  const linkedEmail = useLinkedLabel(linkedUserId, fetchUserEmail);
  const linkedProjectName = useLinkedLabel(linkedProjectId, fetchProjectName);

  const scopeUserId = userId ?? linkedUserId;
  const scopeProjectId = projectId ?? linkedProjectId;
  const statusKey = statuses.join(",");
  const { items, loadMore, loadingMore, hasMore } = useKeysetList<AdminTaskRow>({
    fetchPage: (cursor) =>
      unwrap(
        api.GET("/v1/admin/tasks", {
          params: {
            query: {
              limit: PAGE,
              ...(statuses.length ? { status: statuses } : {}),
              ...(stuck ? { stuck: true } : {}),
              ...(dq ? { q: dq } : {}),
              ...(scopeUserId ? { user_id: scopeUserId } : {}),
              ...(scopeProjectId ? { project_id: scopeProjectId } : {}),
              ...(cursor ? { cursor } : {}),
            },
          },
        }),
      ),
    deps: [dq, statusKey, stuck, scopeUserId, scopeProjectId],
    orderKey: (t) => t.created_at,
    refreshMs: 10_000,
  });

  const searchBox = (
    <SearchBox
      value={q}
      onChange={(v) => set("q", v)}
      placeholder="Search title, goal, or task ID…"
      className={topbarSearch ? "w-56" : undefined}
    />
  );

  return (
    <div className="space-y-4">
      {topbarSearch ? <TopbarPortal>{searchBox}</TopbarPortal> : searchBox}

      <div className="flex min-h-8 flex-wrap items-center gap-2">
        {STATUSES.map((s) => (
          <ToggleChip key={s} active={statuses.includes(s)} onClick={() => toggleStatus(s)}>
            <span className="capitalize">{statusLabel(s)}</span>
          </ToggleChip>
        ))}
        <ToggleChip active={stuck} onClick={() => set("stuck", stuck ? "" : "true")}>
          Stuck
        </ToggleChip>
        {linkedUserId && (
          <ToggleChip active onClick={() => set("user_id", "")}>
            {linkedEmail ?? linkedUserId} <X className="size-3" />
          </ToggleChip>
        )}
        {linkedProjectId && (
          <ToggleChip active onClick={() => set("project_id", "")}>
            {linkedProjectName ?? linkedProjectId} <X className="size-3" />
          </ToggleChip>
        )}
      </div>

      {items === null ? (
        <LoadingState />
      ) : items.length === 0 ? (
        <EmptyState
          icon={ListTodo}
          title={dq ? `No matches for “${dq}”` : "Nothing here"}
          description={dq ? undefined : "No tasks match the current filters."}
        />
      ) : (
        <div className="space-y-3">
          <Rows as="ul">
            {items.map((t) => (
              <li key={t.id} className="relative">
                <div className="hover:bg-accent/50 flex items-start gap-3 px-4 py-3 transition-colors">
                  <StatusMark status={t.status} className="mt-0.5" />
                  <span className="min-w-0 flex-1">
                    {/* The title button stretches over the whole row (its ::after covers
                        the li); the owner link re-raises itself above the overlay so both
                        clicks stay honest - peek on the row, the user page on the email. */}
                    <button
                      type="button"
                      onClick={() => openPeek(t.id)}
                      className="block w-full truncate text-left text-body-sm-regular font-medium after:absolute after:inset-0"
                    >
                      {t.title ?? t.goal_line}
                    </button>
                    <span className="text-muted-foreground block truncate text-caption-md-regular">
                      {t.owner ? (
                        userId ? (
                          t.owner.email
                        ) : (
                          <Link
                            to={`/users/${t.owner.id}`}
                            onClick={(e) => e.stopPropagation()}
                            className="hover:text-foreground relative hover:underline"
                          >
                            {t.owner.email}
                          </Link>
                        )
                      ) : (
                        "no owner"
                      )}{" "}
                      · turn {t.turn} · created {ago(t.created_at)}
                      {t.last_event_at && ` · last event ${ago(t.last_event_at)}`}
                      {t.held && " · held"}
                    </span>
                    {t.stuck_reason && (
                      <span className="text-warning block truncate text-caption-md-regular">
                        stuck - {t.stuck_reason}
                      </span>
                    )}
                  </span>
                  <Money usd={t.spend_usd} className="shrink-0 text-caption-md-regular" muted />
                </div>
              </li>
            ))}
          </Rows>
          <LoadMore hasMore={hasMore} busy={loadingMore} onClick={() => void loadMore()} />
        </div>
      )}

      <AdminTaskPeek
        taskId={peekId}
        seed={items?.find((t) => t.id === peekId)}
        onClose={closePeek}
      />
    </div>
  );
}
