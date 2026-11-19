/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { ArrowRight, Eye, EyeOff } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { Section, StatusMark, taskMark } from "~/components/kit";
import { useTaskWindow, type TaskWindowRow } from "~/components/tasks/task-window";
import { ago } from "~/lib/format";
import { taskHref } from "~/lib/links";
import { STORAGE } from "~/lib/storage";

const HOME_WINDOW = 5;

/** The quiet control voice this section speaks - muted text-caption-md-regular with a size-3 glyph. */
const QUIET =
  "text-muted-foreground hover:text-foreground inline-flex items-center gap-1 text-caption-md-regular transition-colors";

export function RecentTaskList({
  tasks,
  hidden = false,
  onToggle,
}: {
  tasks: TaskWindowRow[];
  hidden?: boolean;
  onToggle?: () => void;
}) {
  // An empty account renders nothing at all - no rows, and no show/hide chrome either;
  // the first-run landing stays pure.
  if (tasks.length === 0) return null;
  if (hidden) {
    // Hidden collapses the whole section to one quiet, self-recovering line - sitting
    // right where "Hide" sat (same px-4 inset), so the toggle doesn't jump under the pointer.
    return (
      <div className="flex justify-end px-4">
        <button onClick={onToggle} aria-expanded={false} className={QUIET}>
          <Eye className="size-3" />
          Show recent
        </button>
      </div>
    );
  }
  return (
    <Section
      // px-4: the rows below inset their content by px-4 (the hover pill's breathing room),
      // so the label and controls align to the rows' text edge, not the container edge.
      headerClassName="px-4"
      title="Recent"
      actions={
        <div className="flex items-center gap-3">
          <Link to="/tasks" className={QUIET}>
            View all
            <ArrowRight className="size-3" />
          </Link>
          {onToggle && (
            <button onClick={onToggle} aria-expanded className={QUIET}>
              <EyeOff className="size-3" />
              Hide
            </button>
          )}
        </div>
      }
    >
      <ul>
        {tasks.map((task) => (
          <li key={task.id}>
            <Link
              to={taskHref({ id: task.id, project_id: task.projectId })}
              className="hover:bg-accent/40 flex items-center gap-3 rounded-md px-4 py-3 transition-colors"
            >
              <StatusMark status={taskMark(task.status, task.ending)} />
              <span className="min-w-0 flex-1 truncate text-body-sm-regular">{task.label}</span>
              <time
                dateTime={task.createdAt}
                className="text-muted-foreground shrink-0 text-caption-md-regular"
              >
                {ago(task.createdAt)}
              </time>
            </Link>
          </li>
        ))}
      </ul>
    </Section>
  );
}

/** Home reads the already-loading shell task window; no route-loader request delays the
 *  composer, and an empty/failed window simply leaves the first-run landing pure.
 *  Hiding is a persisted preference (the task-rail idiom): default shown. */
export function RecentTasks() {
  const { latest } = useTaskWindow();
  const [hidden, setHidden] = useState(
    () => typeof window !== "undefined" && localStorage.getItem(STORAGE.homeRecent) === "hidden",
  );
  const toggle = () =>
    setHidden((h) => {
      localStorage.setItem(STORAGE.homeRecent, h ? "shown" : "hidden");
      return !h;
    });
  return <RecentTaskList tasks={latest.slice(0, HOME_WINDOW)} hidden={hidden} onToggle={toggle} />;
}
