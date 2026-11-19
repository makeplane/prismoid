/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** TaskBrowser - every task list in the app, one component (see data.ts for the layer). */

import { Archive, ArchiveRestore, FolderKanban, Inbox, Trash2, X } from "lucide-react";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router";
import { toast } from "@prismoid/blocks/toast";
import { EmptyState, Rows, SearchBox, Section } from "~/components/kit";
import { TopbarPortal } from "~/components/layout/topbar-slot";
import { useTaskWindow } from "~/components/tasks/task-window";
import { Button } from "@makeplane/propel/components/button";
import {
  AlertDialog,
  AlertDialogActions,
  AlertDialogBody,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogHeader,
  AlertDialogIntro,
  AlertDialogTitle,
} from "@makeplane/propel/components/alert-dialog";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { Button as ButtonElement } from "@makeplane/propel/elements/button";
import { api, unwrap } from "~/lib/api/client";
import { useDebouncedValue, useKeysetList } from "~/lib/paging";
import {
  bucketize,
  fetchTaskCounts,
  fetchTasksPage,
  readBrowserParams,
  REFRESH_MS,
  TASK_GROUPS,
  VIEW_KEY,
  type Counts,
  type GroupBy,
  type GroupDef,
  type ProjectRow,
  type TaskBrowserSeed,
  type TaskFilters,
  type TaskQueryScope,
  type TaskSummary,
  type View,
} from "./data";
import {
  BoardColumn,
  ColumnLoadMore,
  CountLabel,
  emptyCopy,
  ListLoadMore,
  TaskRow,
} from "./pieces";
import { DisplayMenu, FilterChips, FilterMenu } from "./toolbar";

/**
 * Every task list in the app, one browser: search, filters (status group · mode · project),
 * group-by (status · project · mode · none), and two layouts - List and Board. Status grouping
 * is per-group server queries with true GROUP BY counts; other groupings bucket one filtered
 * stream client-side. Boards refuse "none": a board is grouped by definition.
 *
 * `syncToUrl` mirrors state to search params (the /tasks page - a slice is shareable);
 * without it state is component-local (a project's Tasks tab). `projectId` hard-scopes every
 * query and hides the project dimension - inside a project, "which project" is not a question.
 */
export function TaskBrowser({
  projectId,
  projects = [],
  syncToUrl = false,
  searchInTopbar = false,
  seed,
  canStart = true,
}: {
  projectId?: string;
  projects?: ProjectRow[];
  syncToUrl?: boolean;
  /** Section-tab pattern: /tasks projects the search into the topbar;
   *  a project's Tasks tab keeps it inline - the topbar belongs to the page, not a tab. */
  searchInTopbar?: boolean;
  seed?: TaskBrowserSeed;
  /** Can the reader start a task here? A Project viewer cannot, so an empty list
   *  must not invite them to. */
  canStart?: boolean;
}) {
  const [searchParams, setSearchParams] = useSearchParams();
  const [local, setLocal] = useState<Record<string, string>>({});
  const getParam = (key: string) => (syncToUrl ? searchParams.get(key) : (local[key] ?? null));
  /** Set several params in ONE update. Sequential `setSearchParams` calls in a tick each
   *  read the ORIGINAL location, so the last write wins and the others vanish - exactly
   *  the "Clear filters clears only one thing" bug. One batch, one truth. */
  function setParams(pairs: readonly (readonly [string, string | null])[]) {
    if (syncToUrl) {
      setSearchParams(
        (p) => {
          const n = new URLSearchParams(p);
          for (const [key, value] of pairs) {
            if (value === null) n.delete(key);
            else n.set(key, value);
          }
          return n;
        },
        { replace: true },
      );
    } else {
      setLocal((cur) => {
        const n = { ...cur };
        for (const [key, value] of pairs) {
          if (value === null) delete n[key];
          else n[key] = value;
        }
        return n;
      });
    }
  }
  const setParam = (key: string, value: string | null) => setParams([[key, value]]);

  const projectDimension = !projectId && projects.length > 0;
  const parsed = readBrowserParams(getParam);
  // Scope narrows the parse: no project dimension → no project grouping, no project filter.
  const group: GroupBy = parsed.group === "project" && !projectDimension ? "status" : parsed.group;
  const filters: TaskFilters = {
    ...parsed.filters,
    project: projectDimension ? parsed.filters.project : null,
  };

  const [q, setQ] = useState(seed?.q ?? "");
  const dq = useDebouncedValue(q.trim(), 250);

  const paramView = getParam("view");
  const view: View =
    paramView === "board" || paramView === "list"
      ? paramView
      : typeof window !== "undefined" && localStorage.getItem(VIEW_KEY) === "board"
        ? "board"
        : "list";
  // A board is grouped by definition - "none" alone has no board shape (one giant
  // column), so it normalizes to status there. Every real dimension (status, project,
  // mode, date, month) makes columns: buckets are buckets.
  const effGroup: GroupBy = view === "board" && group === "none" ? "status" : group;
  const setView = (v: View) => {
    localStorage.setItem(VIEW_KEY, v);
    // Switching to a board while "none" is chosen would leave the Display menu claiming
    // one grouping and the board showing another - normalize the param in the SAME
    // batched update so URL, menu, and board always agree.
    if (v === "board" && group === "none")
      setParams([
        ["view", v],
        ["group", null],
      ]);
    else setParam("view", v);
  };

  // The URL carries the settled query - a search is shareable, a keystroke is not.
  useEffect(() => {
    if (!syncToUrl) return;
    setSearchParams(
      (p) => {
        const n = new URLSearchParams(p);
        if (dq) n.set("q", dq);
        else n.delete("q");
        return n;
      },
      { replace: true },
    );
  }, [dq, syncToUrl, setSearchParams]);

  const byStatus = effGroup === "status";
  const statusFilter = TASK_GROUPS.find((g) => g.key === filters.statusKey);
  const scope: TaskQueryScope = { q: dq, filters, projectId };
  // `refreshTick` is the bulk actions' reset lever: bumping it re-keys every list + the
  // counts, so archived/deleted rows leave in one clean refetch.
  const [refreshTick, setRefreshTick] = useState(0);
  const filterDeps = [
    dq,
    filters.project,
    filters.mode,
    filters.archived,
    projectId,
    refreshTick,
  ] as const;

  // ── bulk selection ─────────────────────────────────────────────────────────────────────
  const { remove } = useTaskWindow();
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);
  const toggleSelect = (id: string) =>
    setSelected((cur) => {
      const next = new Set(cur);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  // A selection is a claim about the CURRENT list - any change of query drops it.
  useEffect(
    () => setSelected(new Set()),
    [dq, filters.project, filters.mode, filters.statusKey, filters.archived, projectId],
  );

  /** Run one verb over the selection, report honestly, refetch. Per-task failures stay
   *  per-task (a still-running task refuses archive with a 409) - the rest proceed. */
  async function bulk(action: "archive" | "unarchive" | "delete") {
    const ids = [...selected];
    setBulkBusy(true);
    const results = await Promise.allSettled(
      ids.map((id) =>
        action === "delete"
          ? remove(id).then((ok) => {
              if (!ok) throw new Error("delete failed");
            })
          : unwrap(
              api.POST(
                action === "archive"
                  ? "/v1/tasks/{task_id}/archive"
                  : "/v1/tasks/{task_id}/unarchive",
                { params: { path: { task_id: id } } },
              ),
            ),
      ),
    );
    setBulkBusy(false);
    setConfirmingDelete(false);
    setSelected(new Set());
    setRefreshTick((t) => t + 1);
    const failed = results.filter((r) => r.status === "rejected").length;
    const verb = action === "delete" ? "Deleted" : action === "archive" ? "Archived" : "Unarchived";
    if (failed === 0) toast(`${verb} ${ids.length} task${ids.length === 1 ? "" : "s"}.`);
    else
      toast(`${verb} ${ids.length - failed} of ${ids.length}.`, {
        description:
          "Some were refused - live tasks must be cancelled first, and only the creator or project owner may curate.",
      });
  }

  // One keyset list per status group (dormant unless this layout is active). Three
  // explicit calls, not a map over TASK_GROUPS: hooks in a loop are legal only while the
  // array is a module constant, and that invariant should not be one refactor away from
  // a rules-of-hooks violation.
  const groupEnabled = (g: GroupDef) =>
    byStatus && (!filters.statusKey || filters.statusKey === g.key);
  const useGroup = (g: GroupDef, i: number) =>
    useKeysetList<TaskSummary>({
      fetchPage: (cursor) => fetchTasksPage(scope, g.statuses, cursor),
      deps: filterDeps,
      orderKey: (t) => t.created_at,
      refreshMs: REFRESH_MS,
      enabled: groupEnabled(g),
      initial: seed?.pages?.[i] ?? undefined,
    });
  const groupLists = [
    useGroup(TASK_GROUPS[0], 0),
    useGroup(TASK_GROUPS[1], 1),
    useGroup(TASK_GROUPS[2], 2),
  ];
  // …and one filtered stream for every other grouping, bucketed client-side.
  const stream = useKeysetList<TaskSummary>({
    fetchPage: (cursor) => fetchTasksPage(scope, statusFilter?.statuses, cursor),
    deps: [...filterDeps, filters.statusKey],
    orderKey: (t) => t.created_at,
    refreshMs: REFRESH_MS,
    enabled: !byStatus,
    initial: seed?.stream ?? undefined,
  });

  // True totals for the status headers - their own cheap GROUP BY, same filters as the lists.
  const [counts, setCounts] = useState<Counts | null>(seed?.counts ?? null);
  useEffect(() => {
    if (!byStatus) return;
    let live = true;
    const load = () =>
      fetchTaskCounts(scope).then(
        (c) => live && setCounts(c),
        () => {},
      );
    void load();
    const id = window.setInterval(() => {
      if (!document.hidden) void load();
    }, REFRESH_MS);
    return () => {
      live = false;
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- scope is identity-fresh; its parts are the deps
  }, [byStatus, dq, filters.project, filters.mode, projectId]);

  const groupCount = (g: GroupDef) =>
    counts === null ? null : g.statuses.reduce((n, s) => n + (counts[s] ?? 0), 0);

  const statusLists = TASK_GROUPS.map((g, i) => ({
    group: g,
    list: groupLists[i],
    count: groupCount(g),
  })).filter(({ group: g }) => groupEnabled(g));

  const projectName = new Map(projects.map((s) => [s.id, s.name]));
  const buckets = byStatus ? [] : bucketize(stream.items ?? [], effGroup, projectName);

  const allEmpty = byStatus
    ? statusLists.every(({ list }) => list.items !== null) &&
      statusLists.every(({ list }) => list.items?.length === 0)
    : stream.items?.length === 0;

  // Everything currently LOADED and visible - what "Select all" honestly means on a keyset
  // list (the shown count states the scope). Selection is a list-view gesture; boards
  // show no checkboxes, so the shortcut stays quiet there.
  const loadedIds = byStatus
    ? statusLists.flatMap(({ list }) => list.items ?? []).map((t) => t.id)
    : (stream.items ?? []).map((t) => t.id);
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.target instanceof HTMLElement) {
        const tag = e.target.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || e.target.isContentEditable) return;
      }
      // ⌘A claims the shortcut only when there is something to select; Escape clears the
      // selection unless a dialog is up - one keypress must not both dismiss the confirm
      // AND silently drop what was about to be acted on.
      if ((e.metaKey || e.ctrlKey) && e.key === "a" && view === "list" && loadedIds.length > 0) {
        e.preventDefault();
        setSelected(new Set(loadedIds));
      }
      if (e.key === "Escape" && !confirmingDelete) setSelected(new Set());
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const search = (
    <SearchBox
      value={q}
      onChange={setQ}
      placeholder="Search tasks…"
      className={searchInTopbar ? "w-56" : undefined}
    />
  );

  const clearFilters = () =>
    setParams([
      ["status", null],
      ["mode", null],
      ["project", null],
      ["archived", null],
    ]);
  const hasChips = Boolean(
    filters.statusKey || filters.mode || filters.project || filters.archived,
  );

  const filterMenu = (
    <FilterMenu
      filters={filters}
      projects={projectDimension ? projects : []}
      setParam={setParam}
      onClear={clearFilters}
    />
  );
  const displayMenu = (
    // effGroup, not group: the menu states what the screen actually shows (a deep link
    // to a board with group=none reads as Status, which is what renders).
    <DisplayMenu
      group={effGroup}
      view={view}
      projectDimension={projectDimension}
      setParam={setParam}
      setView={setView}
    />
  );
  // the selection bar: what's selected, the verbs, the way out
  const selectionBar = (
    <>
      <span className="text-body-sm-regular font-medium tabular-nums">
        {selected.size} selected
      </span>
      {selected.size < loadedIds.length && (
        // The element, not the ready-made: the label carries a muted count.
        <ButtonElement
          type="button"
          size="sm"
          variant="ghost"
          stretch="auto"
          onClick={() => setSelected(new Set(loadedIds))}
        >
          Select all
          <span className="text-muted-foreground tabular-nums">{loadedIds.length}</span>
        </ButtonElement>
      )}
      <Button
        size="sm"
        stretch="auto"
        variant="secondary"
        icon={<Icon icon={filters.archived ? ArchiveRestore : Archive} />}
        label={filters.archived ? "Unarchive" : "Archive"}
        disabled={bulkBusy}
        onClick={() => void bulk(filters.archived ? "unarchive" : "archive")}
      />
      <Button
        size="sm"
        stretch="auto"
        variant="danger-outline"
        icon={<Icon icon={Trash2} />}
        label="Delete"
        disabled={bulkBusy}
        onClick={() => setConfirmingDelete(true)}
      />
      <IconButton
        size="sm"
        variant="ghost"
        icon={<Icon icon={X} />}
        aria-label="Clear selection"
        onClick={() => setSelected(new Set())}
      />
    </>
  );

  return (
    <div className="space-y-4">
      {searchInTopbar ? (
        // /tasks: EVERY standing control rides the topbar - search, Filter, Display (the
        // route appends "+ New task"). Two lone icons over a dead min-h-8 band was the
        // "weird gap"; in-page there is now only a row WHEN it says something: the
        // selection bar, or the active-filter chips.
        <>
          <TopbarPortal>
            {search}
            {filterMenu}
            {displayMenu}
          </TopbarPortal>
          {(selected.size > 0 || hasChips) && (
            <div className="flex min-h-8 flex-wrap items-center gap-2">
              {selected.size > 0 ? (
                selectionBar
              ) : (
                <FilterChips filters={filters} projectName={projectName} setParam={setParam} />
              )}
            </div>
          )}
        </>
      ) : (
        // embedded (a project's Tasks tab): the topbar belongs to the page, so the row
        // keeps everything inline - search, filter + chips left, display right.
        <div className="flex min-h-8 flex-wrap items-center gap-2">
          {search}
          {selected.size > 0 ? (
            selectionBar
          ) : (
            <>
              {filterMenu}
              <FilterChips filters={filters} projectName={projectName} setParam={setParam} />
            </>
          )}
          <div className="ml-auto flex items-center gap-2">{displayMenu}</div>
        </div>
      )}

      {confirmingDelete && (
        <AlertDialog open onOpenChange={(o) => !o && setConfirmingDelete(false)}>
          <AlertDialogContent>
            <AlertDialogBody>
              <AlertDialogHeader>
                <AlertDialogIntro>
                  <AlertDialogTitle>
                    Delete {selected.size} task{selected.size === 1 ? "" : "s"}?
                  </AlertDialogTitle>
                  <AlertDialogDescription>
                    Everything they produced - artifacts, transcripts, plans - is permanently
                    removed. Your usage history is kept. This cannot be undone.
                  </AlertDialogDescription>
                </AlertDialogIntro>
              </AlertDialogHeader>
            </AlertDialogBody>
            <AlertDialogActions>
              <Button
                variant="ghost"
                size="md"
                stretch="auto"
                label="Cancel"
                onClick={() => setConfirmingDelete(false)}
                disabled={bulkBusy}
              />
              <Button
                variant="danger"
                size="md"
                stretch="auto"
                icon={<Icon icon={Trash2} />}
                label="Delete"
                onClick={() => void bulk("delete")}
                disabled={bulkBusy}
              />
            </AlertDialogActions>
          </AlertDialogContent>
        </AlertDialog>
      )}

      {allEmpty ? (
        <EmptyState icon={Inbox} {...emptyCopy({ q: dq, canStart })} />
      ) : byStatus ? (
        view === "list" ? (
          <div className="space-y-8">
            {statusLists.map(({ group: g, list, count }) =>
              list.items === null || list.items.length === 0 ? null : (
                <Section key={g.key} title={g.label} actions={<CountLabel count={count} />}>
                  <Rows as="ul">
                    {list.items.map((t) => (
                      <TaskRow
                        key={t.id}
                        task={t}
                        selected={selected.has(t.id)}
                        selecting={selected.size > 0}
                        onToggleSelect={() => toggleSelect(t.id)}
                      />
                    ))}
                  </Rows>
                  <ListLoadMore list={list} />
                </Section>
              ),
            )}
          </div>
        ) : (
          <div className="grid items-start gap-4 lg:grid-cols-3">
            {statusLists.map(({ group: g, list, count }) => (
              <BoardColumn
                key={g.key}
                label={g.label}
                dot={g.dot}
                count={count}
                items={list.items}
                footer={<ColumnLoadMore list={list} />}
              />
            ))}
          </div>
        )
      ) : view === "list" ? (
        <div className="space-y-8">
          {effGroup === "none" ? (
            <Rows as="ul">
              {(stream.items ?? []).map((t) => (
                <TaskRow
                  key={t.id}
                  task={t}
                  selected={selected.has(t.id)}
                  selecting={selected.size > 0}
                  onToggleSelect={() => toggleSelect(t.id)}
                />
              ))}
            </Rows>
          ) : (
            buckets.map((b) => (
              <Section
                key={b.key}
                title={b.label}
                actions={<CountLabel count={b.items.length} open={stream.hasMore} />}
              >
                <Rows as="ul">
                  {b.items.map((t) => (
                    <TaskRow
                      key={t.id}
                      task={t}
                      selected={selected.has(t.id)}
                      selecting={selected.size > 0}
                      onToggleSelect={() => toggleSelect(t.id)}
                    />
                  ))}
                </Rows>
              </Section>
            ))
          )}
          <ListLoadMore list={stream} />
        </div>
      ) : (
        <div className="space-y-3">
          <div className="scrollbar-thin flex items-start gap-4 overflow-x-auto pb-2">
            {buckets.map((b) => (
              <BoardColumn
                key={b.key}
                className="w-80 shrink-0"
                label={b.label}
                icon={effGroup === "project" ? FolderKanban : undefined}
                count={b.items.length}
                open={stream.hasMore}
                items={b.items}
              />
            ))}
          </div>
          <ListLoadMore list={stream} />
        </div>
      )}
    </div>
  );
}
