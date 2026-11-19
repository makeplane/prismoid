/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The Routines tab (ADR-056): the standing work this
// bot keeps. Quiet read rows in the shelves' grammar; a row opens the routine on /routines
// (its one management home - no second set of verbs grows here).

import { useEffect, useState } from "react";
import { Link } from "react-router";
import { EmptyState, LoadingState, Pill } from "~/components/kit";
import { api, toastError, unwrap } from "~/lib/api/client";
import { inAgo } from "~/lib/format";
import type { components } from "~/lib/api/types.gen";
import { whenSentence } from "../routines/route";

type RoutineRow = components["schemas"]["RoutineOut"];

const STATUS_TONE = { active: "success", paused: "warning", retired: "muted" } as const;

export function RoutinesTab({ name, projectId }: { name: string; projectId: string | null }) {
  const [rows, setRows] = useState<RoutineRow[] | undefined>(undefined);
  useEffect(() => {
    let alive = true;
    setRows(undefined);
    unwrap(
      api.GET("/v1/bots/{name}/routines", {
        params: { path: { name }, query: { project_id: projectId ?? undefined } },
      }),
    ).then(
      (d) => alive && setRows(d),
      (e) => alive && toastError(e, "Couldn't load the routines."),
    );
    return () => {
      alive = false;
    };
  }, [name, projectId]);

  if (rows === undefined)
    return (
      <div className="px-6 py-4">
        <LoadingState label="Loading routines…" />
      </div>
    );
  if (rows.length === 0)
    return (
      <div className="px-6 py-4">
        <EmptyState
          title="No standing work yet"
          description={`Routines kept by ${name} land here. Create one on the Routines page and pick ${name} as its keeper.`}
        />
        <p className="pt-2 text-center">
          <Link
            to="/routines"
            className="text-muted-foreground hover:text-foreground text-caption-md-regular underline underline-offset-2"
          >
            Open routines…
          </Link>
        </p>
      </div>
    );
  return (
    <div className="scrollbar-thin min-h-0 flex-1 overflow-y-auto px-6 py-4">
      <ul className="ring-foreground/10 overflow-hidden rounded-md ring-1">
        {rows.map((r) => (
          <li key={r.id}>
            <Link
              to={`/routines?routine=${r.id}`}
              className="border-sidebar-border/60 hover:bg-accent/40 flex w-full items-center gap-3 border-b px-4 py-3 text-left text-body-sm-regular transition-colors last:border-b-0"
            >
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium">{r.name}</span>
                <span className="text-muted-foreground block truncate text-caption-md-regular">
                  {whenSentence(r)}
                </span>
              </span>
              {r.next_fire_at && r.status === "active" && (
                <span className="text-muted-foreground shrink-0 text-caption-md-regular">
                  {inAgo(r.next_fire_at)}
                </span>
              )}
              <Pill tone={STATUS_TONE[r.status as keyof typeof STATUS_TONE] ?? "muted"}>
                {r.status}
              </Pill>
            </Link>
          </li>
        ))}
      </ul>
      <p className="text-muted-foreground pt-2.5 text-caption-md-regular">
        Every fire mints an ordinary task, answered by {name} on its direct lane.
      </p>
    </div>
  );
}
