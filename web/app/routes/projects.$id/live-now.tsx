/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The Project lobby's "Live now" (ADR-062): above the task list, the tasks in this Project that
// are moving right now - planning, running or waiting on someone - each with who asked for it
// and, when someone else is running the current turn, whose turn it is. It is the Project's answer
// to "what is everyone doing?" without opening every task.

import { useEffect, useState } from "react";
import { Link } from "react-router";
import { Rows, Section, StatusMark, taskMark } from "~/components/kit";
import { REFRESH_MS } from "~/components/tasks/task-browser/data";
import { api, unwrap } from "~/lib/api/client";
import type { components } from "~/lib/api/types.gen";
import { personName } from "~/lib/format";
import { taskHref } from "~/lib/links";

type TaskRow = components["schemas"]["TaskSummaryOut"];

/** The machine's "moving right now" states: not queued (created), not parked by a person (paused). */
export const LIVE_STATUSES = ["planning", "running", "waiting"] as const;
const LIVE_LIMIT = 20;

export type Live = { tasks: TaskRow[]; more: number };

/** One bounded page, filtered by the server - the newest live tasks of this Project - and, when
 *  the page was full, how many more are live (the server's own tallies: the Project must not read
 *  as quieter than it is). A full page with nothing past it (exactly LIVE_LIMIT live) says no
 *  more; only a tally that FAILS still says there is more, because then nobody knows. */
export async function fetchLiveTasks(projectId: string): Promise<Live> {
  const page = await unwrap(
    api.GET("/v1/tasks", {
      params: {
        query: {
          project_id: projectId,
          status: [...LIVE_STATUSES],
          limit: LIVE_LIMIT,
        },
      },
    }),
  );
  if (!page.next_cursor) return { tasks: page.items, more: 0 };
  try {
    const { counts } = await unwrap(
      api.GET("/v1/tasks/counts", { params: { query: { project_id: projectId } } }),
    );
    const live = LIVE_STATUSES.reduce((n, s) => n + (counts[s] ?? 0), 0);
    return { tasks: page.items, more: Math.max(0, live - page.items.length) };
  } catch {
    return { tasks: page.items, more: 1 };
  }
}

/** "by Ada" and, only when someone else holds the current turn, "turn: Bob". Whose turn it is
 *  is a question of PEOPLE, so it compares ids: two Alexes are two people, and one person under
 *  a changed display name is still one. A name the server could not give is left out rather than
 *  guessed. */
function who(t: TaskRow): string {
  const parts: string[] = [];
  if (t.creator_name) parts.push(`by ${personName(t.creator_name)}`);
  const someoneElse = t.turn_author_id != null && t.turn_author_id !== t.creator_id;
  if (someoneElse && t.turn_author_name) {
    parts.push(`turn: ${personName(t.turn_author_name)}`);
  }
  return parts.join(" · ");
}

/** The section as a pure view. Nothing live renders nothing - no heading over an empty list.
 *  `more` is how many live tasks the page did not hold. */
export function LiveNowRows({ tasks, more }: { tasks: readonly TaskRow[]; more: number }) {
  if (tasks.length === 0) return null;
  return (
    <Section title="Live now">
      <Rows as="ul">
        {tasks.map((t) => (
          <li key={t.id}>
            <Link
              to={taskHref(t)}
              className="hover:bg-accent/50 flex items-center gap-3 px-4 py-3 transition-colors"
            >
              <StatusMark status={taskMark(t.status, t.ending)} />
              <span className="min-w-0 flex-1 truncate text-body-sm-regular">
                {t.title || t.goal}
              </span>
              <span className="text-muted-foreground shrink-0 text-caption-md-regular">
                {who(t)}
              </span>
            </Link>
          </li>
        ))}
      </Rows>
      {more > 0 && (
        <p className="text-muted-foreground px-1 text-caption-md-regular">{`and ${more} more`}</p>
      )}
    </Section>
  );
}

/** Live now for one Project, kept fresh on the task browser's own cadence (a status is a moving
 *  thing; the page that shows it must not be a snapshot from when it was opened). */
export function LiveNow({ projectId }: { projectId: string }) {
  const [live, setLive] = useState<Live>({ tasks: [], more: 0 });

  useEffect(() => {
    let alive = true;
    const load = () =>
      fetchLiveTasks(projectId).then(
        (next) => alive && setLive(next),
        () => {}, // transient: keep what is on screen
      );
    void load();
    const id = window.setInterval(() => {
      if (!document.hidden) void load();
    }, REFRESH_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [projectId]);

  return <LiveNowRows tasks={live.tasks} more={live.more} />;
}
