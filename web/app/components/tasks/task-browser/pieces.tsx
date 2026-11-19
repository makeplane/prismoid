/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The browser's rendering vocabulary: rows, cards, columns, counts, load-more tails. */

import { FolderKanban } from "lucide-react";
import { Link } from "react-router";
import { LoadMore, statusLabel, StatusMark, taskMark } from "~/components/kit";
import { Badge } from "@makeplane/propel/components/badge";
import { Button } from "@makeplane/propel/components/button";
import { Checkbox } from "@makeplane/propel/components/checkbox";
import { ago } from "~/lib/format";
import { taskHref } from "~/lib/links";
import { cn } from "~/lib/utils";
import type { TaskSummary } from "./data";

/** A group's tally. Status groups carry TRUE server totals; bucketed groups (project, mode,
 *  date…) can only count the loaded window - `open` marks that case. The old "1+" marker
 *  read as cryptic ("I don't understand"), so an open count now renders plain
 *  with the caveat in its tooltip; the Load-more tail below the list is the visible signal
 *  that the window (and these tallies) can still grow. */
export function CountLabel({ count, open }: { count: number | null; open?: boolean }) {
  if (count === null) return null;
  return (
    <span
      className="text-muted-foreground text-caption-md-regular tabular-nums"
      title={open ? "Counted from the tasks loaded so far - Load more may grow it" : undefined}
    >
      {count}
    </span>
  );
}

export type ListState = {
  items: TaskSummary[] | null;
  hasMore: boolean;
  loadingMore: boolean;
  loadMore: () => Promise<void>;
};

export function ListLoadMore({ list }: { list: ListState }) {
  return (
    <LoadMore
      className="mt-3"
      hasMore={list.hasMore}
      busy={list.loadingMore}
      onClick={() => void list.loadMore()}
    />
  );
}

export function ColumnLoadMore({ list }: { list: ListState }) {
  if (!list.hasMore) return null;
  return (
    <div className="mt-2">
      <Button
        variant="ghost"
        size="sm"
        stretch="full"
        label={list.loadingMore ? "Loading…" : "Load more"}
        onClick={() => void list.loadMore()}
        disabled={list.loadingMore}
      />
    </div>
  );
}

/** The list row - one line per task, the recipe every task list in the app speaks.
 *  With `onToggleSelect` wired (the browser's bulk mode), the status dot's slot swaps to a
 *  checkbox on hover - and stays a checkbox everywhere once anything is selected, so a
 *  selection in progress is visible as such (Linear's grammar). */
export function TaskRow({
  task,
  selected = false,
  selecting = false,
  onToggleSelect,
}: {
  task: TaskSummary;
  selected?: boolean;
  selecting?: boolean;
  onToggleSelect?: () => void;
}) {
  const swap = onToggleSelect != null;
  const boxVisible = selecting || selected;
  return (
    <li>
      <Link
        to={taskHref(task)}
        className="group/row hover:bg-accent/50 flex items-center gap-3 px-4 py-3 transition-colors"
      >
        <span className="relative flex size-4 shrink-0 items-center justify-center">
          {swap && (
            <span
              role="presentation"
              className={cn(
                "absolute inset-0 items-center justify-center",
                boxVisible ? "flex" : "hidden group-hover/row:flex",
              )}
              onClick={(e) => {
                // the checkbox lives inside the row's Link: select, never navigate
                e.preventDefault();
                e.stopPropagation();
                onToggleSelect();
              }}
            >
              <span className="pointer-events-none flex">
                <Checkbox checked={selected} aria-label={`Select ${task.title || task.goal}`} />
              </span>
            </span>
          )}
          <StatusMark
            status={taskMark(task.status, task.ending)}
            className={cn("shrink-0", swap && (boxVisible ? "hidden" : "group-hover/row:hidden"))}
            decorative
          />
        </span>
        <span className="min-w-0 flex-1 truncate text-body-sm-regular">
          {task.title || task.goal}
        </span>
        {task.mode !== "standard" && <Badge variant="neutral" size="xs" label={task.mode} />}
        <span className="text-muted-foreground w-24 shrink-0 text-right text-caption-md-regular capitalize">
          {statusLabel(taskMark(task.status, task.ending))}
        </span>
        <span className="text-muted-foreground w-16 shrink-0 text-right text-caption-md-regular">
          {ago(task.created_at)}
        </span>
      </Link>
    </li>
  );
}

/** A board column states a group; a card states a task. Columns are facts, not drop
 *  targets - status belongs to the machine, so there is deliberately no drag. */
export function BoardColumn({
  label,
  dot,
  icon: Icon,
  count,
  open,
  items,
  footer,
  className,
}: {
  label: string;
  dot?: string;
  icon?: typeof FolderKanban;
  count: number | null;
  open?: boolean;
  items: TaskSummary[] | null;
  footer?: React.ReactNode;
  className?: string;
}) {
  return (
    <section aria-label={label} className={cn("bg-muted/40 min-w-0 rounded-lg p-2", className)}>
      <header className="flex items-center gap-2 px-2 py-1.5">
        {/* the mark animates only when the column HOLDS something live - an empty
            "In progress" spinning forever is motion with nothing to report */}
        {dot && <StatusMark status={dot} decorative still={(count ?? items?.length ?? 0) === 0} />}
        {Icon && <Icon className="text-muted-foreground size-3.5" />}
        <h2 className="min-w-0 truncate text-body-sm-regular font-medium">{label}</h2>
        <CountLabel count={count} open={open} />
      </header>
      {items === null ? (
        <p className="text-muted-foreground px-2 py-6 text-center text-caption-md-regular">
          Loading…
        </p>
      ) : items.length === 0 ? (
        <p className="text-muted-foreground px-2 py-6 text-center text-caption-md-regular">
          No tasks
        </p>
      ) : (
        <ul className="space-y-2">
          {items.map((t) => (
            <TaskCard key={t.id} task={t} />
          ))}
        </ul>
      )}
      {footer}
    </section>
  );
}

export function TaskCard({ task }: { task: TaskSummary }) {
  return (
    <li>
      <Link
        to={taskHref(task)}
        className="ring-foreground/10 bg-card hover:bg-accent/50 block rounded-md p-3 ring-1 ring-inset transition-colors"
      >
        <p className="line-clamp-2 text-body-sm-regular font-medium">{task.title || task.goal}</p>
        <p className="text-muted-foreground mt-2 flex items-center gap-2 text-caption-md-regular">
          <span className="capitalize">{statusLabel(taskMark(task.status, task.ending))}</span>
          {task.mode !== "standard" && <Badge variant="neutral" size="xs" label={task.mode} />}
          <span className="ml-auto">{ago(task.created_at)}</span>
        </p>
      </Link>
    </li>
  );
}

/** What an empty task list says. "Start something new." is an invitation, so it is only said to
 *  someone who can accept it; a Project viewer is told what will appear instead. */
export function emptyCopy({ q, canStart }: { q: string; canStart: boolean }): {
  title: string;
  description: string | undefined;
} {
  if (q) return { title: `No matches for “${q}”`, description: undefined };
  return {
    title: "Nothing here yet",
    description: canStart ? "Start something new." : "Tasks started in this Project show up here.",
  };
}
